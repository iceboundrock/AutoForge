"""ControllerEngine: deterministic orchestration over agent CLIs.

Core primitive is :meth:`step` (exactly one phase execution); :meth:`run`
loops ``step()`` until a STOP phase. Transition topology lives in
``transitions.py``; this engine applies it and, crucially, **verifies every
agent claim against GitHub** before acting on it:

- ANALYZE_EXECUTE: the agent commits on the worktree's detached HEAD and
  reports it; the controller checks that HEAD itself (detached, the reported
  SHA, descending from the default branch head and from the branch head it
  recorded, every published commit message within policy), then pushes it to
  ``autoforge/<n>`` and opens the PR (or adopts the open one already on that
  branch) as journalled effects, and reads the PR back: open, this repo,
  at exactly the pushed HEAD (ADR 0004, #161).
- REVIEW: the review is bound to the PR HEAD fetched *before* the review;
  the reported round, SHA and comment (existence, PR membership, round
  marker, reviewed-HEAD marker) are checked; the HEAD is re-fetched after
  the review to detect drift.
- FIX: previous/new HEAD match reality, every open finding has exactly one
  resolution, follow-up issues really exist in this repository.
- READY_FOR_MERGE / MERGE: **the controller merges, never an agent.** Both
  phases run the same controller-side pre-merge verification against
  GitHub (fail closed): the state carries a clean review bound to a HEAD,
  the PR is OPEN in this repository at exactly that HEAD, is not a draft,
  changes none of ``safety.protected_merge_paths`` (a PR that edits the
  workflow files defining the hosted checks also edits what a green check
  means, so it is never merged unattended),
  every check succeeded, each ``safety.required_checks`` context came from
  a GitHub Actions run at that HEAD whose job/step structure equals the
  base branch's own run of the same workflow (``verify_check_definition``:
  a green check produced by a different definition is not the check the
  gate trusts), ``mergeable`` is MERGEABLE, ``mergeStateStatus``
  is CLEAN/HAS_HOOKS, no auto-merge is armed and the base branch has no
  merge queue, and the review comment the state names is re-read from
  GitHub and must still be this round's ``ai-review-result`` marker at
  that HEAD, base and merge base on that PR saying ``needs_fix_round:
  false`` (the clean review is the one fact the gate would otherwise take
  from the state file alone, #94). Only then, because it executes the PR's
  code, the reviewed commit is exported into a private temporary directory
  (never the operator's checkout, never a worktree) and
  ``merge.verification_commands`` run there; any failure -> BLOCKED.
  Conclusive negatives -> BLOCKED; HEAD, base or merge-base drift ->
  REVIEW; inconclusive data raises and keeps the phase, re-checked on
  ``resume`` at most ``merge.max_verification_attempts`` times, then
  BLOCKED. Before any of that, the clean review must be bound
  to the PR being merged: the review records the PR it was posted on
  (``reviewed_pr_url``), the HEAD and the base branch, and both phases
  require ``current_pr_url`` -- and the PR GitHub returns for it -- to be
  that PR by identity (same repository and number), else BLOCKED; the
  binding is never moved to another PR, not even one at the reviewed HEAD.
  MERGE then runs ``gh pr merge`` bound to the reviewed HEAD
  (``--match-head-commit``) and counts the merge only after GitHub reports
  the PR as ``MERGED`` at that HEAD into the reviewed base. If that call
  left auto-merge armed, the controller disarms it. No prompt is rendered
  and no provider is invoked.
- UPDATE_EPIC: ``next_issue_url`` is verified exactly like the first issue
  in INITIALIZING before the controller switches issues: it parses as an
  issue URL of this repository, is neither the EPIC nor the issue just
  finished, exists on GitHub and is OPEN. A rejected selection keeps the
  phase (the agent is asked again once, with the controller's reason); a
  second rejection enters BLOCKED. Only a verified issue reaches
  ANALYZE_EXECUTE.

Loop bounds (``workflow:`` config, enforced from persisted state so
``resume`` never resets them; see ``loop_guard.py``):
- ``max_review_rounds``: a review round at the cap that still has findings
  is BLOCKED instead of starting a FIX whose result could never be
  reviewed; review round cap+1 never starts (checked before REVIEW binds a
  HEAD or invokes an agent, whatever path led there).
- stagnation: consecutive rounds with findings whose required resolutions
  are identical (``stagnation_identical_rounds``), or whose finding count
  never changed while some required resolution recurs
  (``stagnation_unchanged_count_rounds``) -> BLOCKED. Rounds of all-new
  findings are progress and only meet the cap.
- ``max_total_steps``: the run's cumulative ``step_count`` (all issues, all
  phases, across ``resume``) -> BLOCKED before another step executes. One
  exception: a REPLAN_REEXECUTE journal past the destructive write
  (SUPERSEDE_INTENT, COMPENSATING, SUPERSEDED) is finished first -- the
  step counts, no agent runs, nothing is closed -- so the source PR the
  controller closed is never stranded unrecorded; the budget ends the run
  at the next phase boundary (``replan_txn.budget_may_stop``, #71).
Failed invocations never consume a round or a history entry.

Safety rules:
- dry-run is fully side-effect-free: no agent subprocess, no gh call, no
  state/log writes, no lock.
- READY_FOR_MERGE is a holding state. MERGE requires BOTH
  ``safety.allow_merge=true`` in config AND ``--allow-merge`` on the CLI.
- Business failures (agent reports failure/blocked, verification mismatch,
  ambiguous recovery) never trigger blind retries. Only a malformed
  CONTROL_RESULT with exit code 0 gets a bounded correction attempt.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from collections.abc import Callable, Hashable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn, overload

from . import __prompt_version__
from .claims import (
    FOLLOW_UP,
    IMPLEMENTATION,
    PROGRESS,
    REVIEW,
    Claimants,
    Collection,
    FollowUpClaim,
    Holder,
    ImplementationClaim,
    ProgressClaim,
    ReviewClaim,
    collect,
    render_follow_up_marker,
    render_implementation_marker,
    render_progress_marker,
    render_review_marker,
    scan,
)
from .config import (
    DEFAULT_STATE_DIR,
    AgentLimits,
    AutoForgeConfig,
    LoopDetectionConfig,
    validate_required_profiles,
)
from .effect_ops import (
    ProgressCommentOp,
    ReviewCommentOp,
    append_problem,
    drive,
    operation_for,
)
from .effects import (
    FOLLOW_UP_SOURCE_ENTRY,
    MAX_BODY_CHARS,
    MAX_EFFECT_STATE_CHARS,
    AnalyzeContext,
    EffectKind,
    EffectOwner,
    EffectRecord,
    EntryObservation,
    FixContext,
    ReviewContext,
    Stage,
    UpdateEpicContext,
    compose_append,
    follow_up_issue_body,
    implementation_closing_block,
    is_legacy_reentry,
    launch_label_for,
    plan_size_problem,
    progress_comment_body,
    review_comment_body,
    review_comment_problem,
    sha256_text,
)
from .errors import (
    CheckoutDriftError,
    ClaimConflictError,
    ConfigurationError,
    ControlResultError,
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    GitTransportError,
    LockError,
    StateError,
    StateTransitionError,
    VerificationError,
)
from .executor import (
    DEFAULT_MAX_OUTPUT_BYTES,
    LIMIT_IDLE,
    LIMIT_MAX_RUNTIME,
    MAX_DEADLINE_SECONDS,
    ExecutionRequest,
    execute,
)
from .git_transport import (
    GitRemote,
    GitTransport,
    local_git_request,
    published_range_problem,
    valid_branch_name,
)
from .github import (
    CommentInfo,
    GitHubClient,
    IssueInfo,
    PRInfo,
    WorkflowRunJobs,
    build_merge_argv,
)
from .local_workspace import (
    GIT_TIMEOUT_SECONDS,
    FeatureSpec,
    LocalWorkspace,
    WorkspaceSnapshot,
    read_feature_spec,
    verify_feature_spec_unchanged,
)
from .locking import ControllerLock, repository_lock_path
from .loop_detect import LIMIT_LOOP
from .loop_guard import (
    RESULT_CLEAN,
    RESULT_NEEDS_FIX,
    RESULT_STALE,
    next_round_cap_reason,
    review_record,
    round_cap_reason,
    stagnation_reason,
    step_budget_reason,
    truncated_evidence_rounds,
)
from .premerge import (
    commit_is_local,
    describe_definition_difference,
    export_commit_tree,
    fetch_pr_head,
)
from .profiles import REQUIRED_PROFILES, local_required_profiles, profile_for_phase
from .progress import ProgressReporter
from .prompts import (
    COMMON_TEMPLATE,
    LOCAL_COMMON_TEMPLATE,
    TEMPLATE_FILES,
    escape_inline,
    fenced_untrusted_block,
    load_template,
    render,
    render_phase,
)
from .providers import AgentExecutionResult, AgentRequest, ProviderRegistry
from .redaction import redact, redact_argv, redact_dict
from .replan import HistoricalReviewCollector, ReplanDecision, evaluate_replan_policy
from .replan_txn import (
    CLOSE_BEGUN_STAGES,
    MAX_CLOSE_ATTEMPTS,
    Disposition,
    ReplanStage,
    ReplanTransaction,
    budget_may_stop,
    find_non_open_claimant,
    has_close_receipt,
    may_invoke_agent,
    new_transaction_id,
    render_close_receipt,
    select_bound_candidate,
    verify_attestation,
    verify_close_never_ran,
    verify_closed_source,
    verify_decision_point,
    verify_run_binding,
    verify_sole_implementation_claimant,
    verify_source_checkpoint,
    verify_target_marker,
    verify_target_pr,
)
from .result_parser import (
    MAX_CONTROL_RESULT_CHARS,
    MAX_FINDING_ID_CHARS,
    MAX_FINDING_LOCATION_CHARS,
    MAX_FINDING_RESOLUTION_CHARS,
    MAX_FINDING_TITLE_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FIX_RATIONALE_CHARS,
    MAX_FOLLOW_UP_BODY_CHARS,
    MAX_FOLLOW_UP_TITLE_CHARS,
    MAX_PR_BODY_CHARS,
    MAX_PR_TITLE_CHARS,
    MAX_PROGRESS_CHARS,
    MAX_RESOLUTIONS_PER_FIX,
    MAX_REVIEW_SECTION_CHARS,
    MAX_REVIEW_SUMMARY_CHARS,
    MAX_ROADMAP_SECTION_CHARS,
    MIN_RATIONALE_CHARS,
    AnalyzeExecuteResult,
    FindingResolution,
    FixResult,
    LocalAnalyzeExecuteResult,
    LocalFixResult,
    LocalReviewResult,
    ReplanReexecuteResult,
    ReviewResult,
    UpdateEpicRequest,
    UpdateEpicResult,
    commit_message_problem,
    parse_control_result,
    published_payload_problem,
)
from .roadmap import (
    ROADMAP_END_MARKER,
    ROADMAP_START_MARKER,
    RoadmapError,
    RoadmapSplit,
    splice_roadmap,
    split_roadmap,
)
from .run_contract import LocalRunContract, validate_local_run_contract
from .runlog import ExecutionRecord, RunLogger, StepLog
from .safefs import SafeRoot
from .state import (
    STATE_FILENAME,
    AutoForgeState,
    StatePaths,
    bound_block_reason,
    load_state,
    no_state_error,
    quarantine_state_file,
    save_state,
    utcnow_iso,
)
from .transitions import (
    LOCAL_WRITE_PHASES,
    STOP_PHASES,
    TERMINAL_PHASES,
    Phase,
    WorkflowMode,
    decide_next_phase,
    edges_for,
    stop_phases_for,
    validate_transition,
)
from .validation import (
    GitHubIssueRef,
    GitHubPullRequestRef,
    parse_comment_url,
    parse_issue_url,
    parse_pr_url,
    same_issue_url,
    same_pr_url,
    validate_epic_and_issue,
)

# The payload bounds the parser enforces, rendered into the prompts so the
# agent is told the contract it will be held to: the REVIEW field bounds in
# the review prompts, the whole-block bound in the common instructions. The
# templates carry no literal numbers: the value the prompt states is the
# value ``result_parser`` rejects against.
REVIEW_BOUND_VARIABLES: dict[str, str | int | None] = {
    "MAX_CONTROL_RESULT_CHARS": MAX_CONTROL_RESULT_CHARS,
    "MAX_FINDINGS_PER_REVIEW": MAX_FINDINGS_PER_REVIEW,
    "MAX_FINDING_RESOLUTION_CHARS": MAX_FINDING_RESOLUTION_CHARS,
    "MAX_FINDING_TITLE_CHARS": MAX_FINDING_TITLE_CHARS,
    "MAX_FINDING_LOCATION_CHARS": MAX_FINDING_LOCATION_CHARS,
    "MAX_FINDING_ID_CHARS": MAX_FINDING_ID_CHARS,
    "MAX_REVIEW_SECTION_CHARS": MAX_REVIEW_SECTION_CHARS,
    "MAX_REVIEW_SUMMARY_CHARS": MAX_REVIEW_SUMMARY_CHARS,
    "MAX_REVIEW_COMMENT_CHARS": MAX_BODY_CHARS,
}
# The FIX payload bounds (#77), stated to the fixer the same way.
FIX_BOUND_VARIABLES: dict[str, str | int | None] = {
    "MAX_RESOLUTIONS_PER_FIX": MAX_RESOLUTIONS_PER_FIX,
    "MAX_FIX_RATIONALE_CHARS": MAX_FIX_RATIONALE_CHARS,
    "MIN_RATIONALE_CHARS": MIN_RATIONALE_CHARS,
    "MAX_FOLLOW_UP_TITLE_CHARS": MAX_FOLLOW_UP_TITLE_CHARS,
    "MAX_FOLLOW_UP_BODY_CHARS": MAX_FOLLOW_UP_BODY_CHARS,
}

# How many times one LOCAL phase entry may launch a write-capable agent
# before the run blocks. Every launch is charged and checkpointed before the
# agent starts (see ``AutoForgeState.local_pending_phase``) -- the phase
# entry's first launch and each correction retry after a malformed
# CONTROL_RESULT alike (R9-F3) -- so a crash, a malformed result or a failing
# validation command all leave a record that ``resume`` continues from, and
# ``execution.max_correction_attempts`` can never multiply this bound.
# Without it, a phase that can never be verified would be re-invoked by every
# ``resume`` forever.
MAX_LOCAL_PHASE_ATTEMPTS = 3


MERGE_GATE_MESSAGE = (
    "Automatic merge is disabled in this milestone: MERGE requires BOTH config "
    "'safety.allow_merge: true' AND the CLI flag '--allow-merge'."
)

# How many times UPDATE_EPIC may select a next issue the controller rejects
# (does not exist, CLOSED, other repository, the EPIC or the current issue)
# before the run is BLOCKED: the first rejection is retried once with the
# reason rendered into the prompt, the second one is final.
MAX_NEXT_ISSUE_SELECTIONS = 2

# `mergeStateStatus` values under which the controller is willing to call
# `gh pr merge`. Everything else is a conclusive "not now" (BLOCKED) except
# UNKNOWN/"" which is inconclusive (stay in MERGE, re-check on resume).
MERGEABLE_STATE_STATUSES = frozenset({"CLEAN", "HAS_HOOKS"})
MERGE_STATE_HINTS = {
    "BLOCKED": "branch protection is not satisfied (required reviews/checks)",
    "BEHIND": "the head branch is behind the base branch and must be updated (new HEAD -> REVIEW)",
    "DIRTY": "merge conflicts with the base branch",
    "DRAFT": "the PR is a draft",
    "UNSTABLE": "a non-required check is failing",
}

# -- durable markers -------------------------------------------------------
#
# An agent's GitHub write is identified to the controller by a marker the
# agent embeds in it. ``autoforge.claims`` owns the whole contract (exact
# payload schemas, renderers, the scan, explicit cardinality); the engine
# only decides what a :class:`ClaimConflictError` means where it is raised:
# at a phase entry it is ``BLOCKED`` without launching an agent, on a
# post-agent read-back it is a :class:`VerificationError`.


def _follow_up_pairs(
    grouped: dict[Hashable, tuple[Holder[IssueInfo, FollowUpClaim], ...]],
) -> list[tuple[str, str]]:
    """``(finding id, issue URL)`` for every holder, ordered by finding id then URL."""
    return sorted((h.claim.finding_id, h.obj.url) for hs in grouped.values() for h in hs)


def _finding_what(pr_ref: GitHubPullRequestRef, finding_id: str) -> str:
    return f"finding {finding_id} of PR {pr_ref.canonical}"


def _same_pr(a: str, b: str) -> bool:
    """Whether two PR URLs name one PR, ``""`` standing for no PR on either side."""
    if not a or not b:
        return not a and not b
    return same_pr_url(a, b)


def _with_leftovers(message: str, leftovers: str) -> str:
    """``message`` followed by what the invocation left behind, when anything.

    ``leftovers`` is the executor's sentence (``ExecutionResult.leftovers``):
    empty for a clean exit or a clean kill, otherwise the fact an operator
    needs next to a timeout or a failed exit -- that a member of the process
    group survived SIGKILL, or that a writer beyond the kill's reach still
    holds the pipes -- so "was killed" is never read as "is gone" (#85).
    """
    return f"{message} ({leftovers})" if leftovers else message


def _limit_reached(limits: AgentLimits, result: AgentExecutionResult) -> str:
    """Which limit stopped a timed-out agent, as a predicate (#193).

    ``made no progress for 900s (last activity 08:31:02 UTC)`` when it had
    written nothing for its idle limit; the wall-clock ceiling it reached,
    its ``max_runtime_seconds`` or the executor's one-week backstop when it
    has none; ``timed out`` with both limits when the provider did not say
    which fired.
    """
    if result.timeout_limit == LIMIT_IDLE and limits.idle_timeout_seconds is not None:
        if result.last_activity_at:
            seen = datetime.fromisoformat(result.last_activity_at).astimezone(UTC)
            last = f"last activity {seen:%H:%M:%S} UTC"
        else:
            last = "no output since launch"
        return f"made no progress for {limits.idle_timeout_seconds}s ({last})"
    if result.timeout_limit == LIMIT_MAX_RUNTIME:
        if limits.max_runtime_seconds is None:
            return f"reached the {MAX_DEADLINE_SECONDS}s (one week) runtime backstop"
        return f"reached its {limits.max_runtime_seconds}s maximum runtime"
    return f"timed out ({limits.describe()})"


def _loop_killed(result: AgentExecutionResult) -> str:
    """What the loop detector killed a timed-out agent for (#194); '' when it did not."""
    if result.timeout_limit != LIMIT_LOOP:
        return ""
    return result.loop.describe() if result.loop is not None else "it repeated itself"


def generate_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"af-{stamp}-{secrets.token_hex(3)}"


# A local run never creates a PR or an Issue, so a phase whose only purpose
# is a GitHub side effect has no local template at all.
LOCAL_PHASE_TEMPLATE: dict[Phase, str | None] = {
    Phase.INITIALIZING: None,  # deterministic: freeze the spec, read the tree
    Phase.ANALYZE_EXECUTE: "local_analyze_execute.md",
    Phase.REVIEW: "local_review.md",
    Phase.FIX: "local_fix.md",
    Phase.DONE: None,
    Phase.BLOCKED: None,
    Phase.FAILED: None,
}


PHASE_TEMPLATE: dict[Phase, str | None] = {
    Phase.INITIALIZING: None,  # deterministic, no agent call
    Phase.ANALYZE_EXECUTE: "analyze_execute.md",
    Phase.REVIEW: "review.md",
    Phase.FIX: "fix.md",
    Phase.REPLAN_REEXECUTE: "replan_reexecute.md",
    Phase.READY_FOR_MERGE: None,  # holding state, no agent call
    Phase.MERGE: None,  # controller runs `gh pr merge` itself; agents never merge
    Phase.UPDATE_EPIC: "update_epic.md",
    Phase.DONE: None,
    Phase.BLOCKED: None,
    Phase.FAILED: None,
}

# The UPDATE_EPIC prompt once the progress comment is published (ADR 0004
# D4.7, D13.7): it asks only for the input a later controller step rejected.
UPDATE_EPIC_RE_REQUEST_TEMPLATE = "update_epic_rerequest.md"


@dataclass
class StepPlan:
    phase: str
    profile_name: str
    provider: str
    model: str
    effort: str
    command: list[str]
    # The agent's limits (#193); ``None`` for a deterministic plan.
    limits: AgentLimits | None
    prompt_length: int
    prompt_preview: str
    prompt_full: str
    routing: str
    template: str = ""
    review_round: int = 0
    variables: dict[str, str] = field(default_factory=dict)
    expected_next: str = ""
    legal_next: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # How the agent is watched for a loop (#194); ``None`` for a deterministic plan.
    loop_detection: LoopDetectionConfig | None = None


@dataclass
class StepOutcome:
    run_id: str
    previous_phase: str
    next_phase: str
    dry_run: bool
    plan: StepPlan | None = None
    result: dict | None = None
    message: str = ""


# Bound on the operator's ``unblock --reason`` text: it is persisted in
# ``state.json`` and in the run log, so it is kept to a note, not a document.
MAX_UNBLOCK_REASON_CHARS = 1000


@dataclass(frozen=True)
class UnblockDecision:
    """What an unblock would do, decided from state plus live GitHub reads.

    ``target`` is the phase to re-enter, or ``None`` when the controller
    still cannot determine a safe one (the run stays BLOCKED). ``detail``
    says why, in either case. ``pr`` is the live PR when one is bound, and
    ``carry_findings`` marks a REVIEW target that the persisted review
    evidence does not describe (the revision moved, or no review of this
    PR at this revision is bound): the last result, if any, is marked stale
    and the open findings, if any, become the findings that review is told
    to re-check; a carry an earlier stale round already made is preserved.
    """

    target: Phase | None
    detail: str
    pr: PRInfo | None = None
    carry_findings: bool = False

    @property
    def refused(self) -> bool:
        return self.target is None


@dataclass(frozen=True)
class _AnalyzeEntry:
    """What an ANALYZE_EXECUTE entry read before the launch (#161).

    ``base_sha`` is the default branch head the candidate must descend from
    and ``remote_head`` the head of the controller's branch (``None`` when it
    does not exist), the push's expected old value. ``adopt_url`` is the
    open, unmarked PR already on that branch, which the controller adopts
    instead of opening a second one (ADR 0004 D9.4). In memory only: the
    entry observation persists the default branch at the base, the branch
    head and the PR to adopt, and the default branch is read again before
    the plan is journaled and before its PR is created, so a rename since
    the entry fails closed.
    """

    issue_url: str
    default_branch: str
    base_sha: str
    remote_head: str | None
    adopt_url: str = ""


@dataclass
class UnblockOutcome:
    """Result of :meth:`ControllerEngine.unblock` for the CLI."""

    run_id: str
    unblocked: bool
    phase: str  # the run's phase after the command: the target, or BLOCKED
    message: str
    dry_run: bool = False


def local_state_paths(
    workspace: LocalWorkspace,
    *,
    explicit: str | Path | None = None,
    configured: str | Path | None = None,
) -> StatePaths:
    """Where a LOCAL run keeps its runtime state.

    A LOCAL run fingerprints *every* entry in the working tree, so its own
    state directory cannot live there: it would invalidate its own fingerprint
    on every step, or have to be carved out of it by path -- a region an agent
    could then write into without moving the fingerprint. The default is
    therefore ``<git dir>/autoforge/state``. The git directory is already
    outside the reviewed tree for reasons that have nothing to do with
    AutoForge, and it is the natural anchor: it is the deepest directory in
    the chain the controller did not create, so every component below it is
    opened ``O_NOFOLLOW`` from a descriptor (see
    :class:`autoforge.state.StatePaths`).

    ``explicit`` is ``--state-dir`` and is honoured as given. ``configured``
    is ``config.state_dir``, which is REMOTE's setting: a value left at its
    default is not a choice, and anything else is. Either way the caller
    checks the result against the working tree; this function only decides
    *which* directory is meant.
    """
    chosen = explicit or (
        configured if configured and str(configured) != DEFAULT_STATE_DIR else None
    )
    if chosen is not None:
        return StatePaths.from_state_dir(chosen)
    git_dir = workspace.git_dirs()[0]
    return StatePaths.from_state_dir(git_dir / "autoforge" / "state", anchor=git_dir)


# What a REMOTE re-entry does before the phase's agent is launched again,
# quoted in the error of an invocation interrupted after the agent returned.
# Every REMOTE phase that launches an agent has an entry, because every one
# of them reconciles with GitHub in :meth:`ControllerEngine._remote_entry`
# before launching; the table is complete by construction (a test holds it
# against ``PHASE_TEMPLATE``), so no phase is ever described as "relaunched".
_REMOTE_REENTRY_RECONCILIATION: dict[Phase, str] = {
    Phase.ANALYZE_EXECUTE: (
        "completes the push and the PR from the persisted plan when it was saved, and "
        "otherwise recovers an open PR carrying the issue's implementation marker only "
        "when this entry has not launched, refusing one it did not journal, instead of "
        "relaunching the agent"
    ),
    Phase.REVIEW: (
        "completes the round from the persisted review comment plan when it was saved, "
        "and otherwise refuses a review comment for this round at this revision that it "
        "did not journal, instead of relaunching the reviewer"
    ),
    Phase.FIX: (
        "completes the follow-up issues, marker appends and push from the persisted plan when "
        "it was saved, and otherwise routes a HEAD already pushed past the reviewed one back "
        "to REVIEW and refuses a follow-up issue it did not journal, instead of relaunching "
        "the fixer"
    ),
    Phase.REPLAN_REEXECUTE: "replays the persisted replan transaction",
    Phase.UPDATE_EPIC: (
        "completes from the persisted progress comment, roadmap section and selection "
        "when they were saved, and otherwise refuses a progress comment it did not "
        "journal, instead of relaunching the agent for a second one"
    ),
}


class ControllerEngine:
    def __init__(
        self,
        config: AutoForgeConfig,
        state_dir: str | Path | None = None,
        workdir: str | Path = ".",
        runner=None,
        github: GitHubClient | None = None,
        providers: ProviderRegistry | None = None,
    ) -> None:
        self.config = config
        self._paths = StatePaths.from_state_dir(state_dir or config.state_dir or ".autoforge")
        # The state directory as a capability, opened once per run by
        # :meth:`state_root` and held for the engine's lifetime: every
        # controller read and write of run state goes through this descriptor,
        # and the pathname is only ever *re-verified* against it, never
        # re-resolved into a new binding.
        self._state_root: SafeRoot | None = None
        self.workdir = str(workdir)
        self.providers = providers or ProviderRegistry(runner=runner)
        # The GitHub client is built on first *use*, never on construction: a
        # LOCAL run must reach DONE without a GitHubClient ever existing, and
        # `local doctor` must work with no gh binary and no authentication.
        # An injected client (tests) is used as given.
        self._github = github
        self._runner = runner
        # The remote the controller's own git transport talks to (ADR 0004
        # D7.1): ``https://github.com/<repository>.git`` unless a test
        # injects a ``file://`` one. Never ``origin``, never configuration.
        self._git_remote: GitRemote | None = None
        # The ANALYZE_EXECUTE entry's reads (see :meth:`_reconcile_analyze_entry`)
        # and the candidate its result check accepted; re-derived on every
        # entry, never trusted across one.
        self._analyze_entry: _AnalyzeEntry | None = None
        self._analyze_candidate = ""
        self._workspace: LocalWorkspace | None = None
        # The per-issue agent worktree paths already derived (a git read
        # each), keyed by the worktree's name; see :meth:`agent_worktree_path`.
        self._agent_worktrees: dict[str, Path] = {}
        # The workspace snapshot bound immediately before a LOCAL agent
        # phase. The prompt is rendered from it, so the fingerprint the
        # reviewer is told to report is exactly the one persisted in state.
        self._local_bound_snapshot: WorkspaceSnapshot | None = None
        # True while the LOCAL phase being invoked is a *retry* of an
        # invocation that was already launched once (see
        # ``AutoForgeState.local_pending_phase``); the prompt then tells the
        # agent that work from the earlier attempt may already be present.
        self._local_resumed_invocation = False
        # Likewise for the FIX entry: the open follow-up issues it found
        # already carrying a marker for one of the open findings (rendered
        # into ``FOLLOW_UP_ISSUES``), and the candidate its result check
        # accepted (#163). Re-derived on every entry, never persisted: the
        # entry observation and the completion context are what persist.
        self._existing_follow_ups: dict[str, str] = {}
        self._fix_candidate = ""
        # The progress comment an UPDATE_EPIC entry adopted without a record:
        # posted by an agent under the previous contract (ADR 0004 D13.7), or
        # recorded as pre-existing by the entry observation of that adoption.
        # Set only by :meth:`_reconcile_update_epic_entry`; it makes the launch
        # a selection re-request, which never carries progress text.
        self._adopted_progress_comment_url = ""
        # The EPIC body the UPDATE_EPIC entry read, split around its managed
        # roadmap section (``None`` until the entry has read it: a plan or dry
        # run renders a placeholder). The current section is handed to the
        # agent as ``CURRENT_ROADMAP_SECTION``, and the completion context
        # stores the digests of its outside bytes (D4.6), which the splice
        # requires before it writes. Re-derived from GitHub before every
        # launch, never persisted itself.
        self._epic_roadmap_at_entry: RoadmapSplit | None = None
        # The follow-up issues already open for this PR from earlier rounds,
        # as (finding id, issue URL): found by the REVIEW and FIX entries and
        # rendered into ``EXISTING_FOLLOW_UP_ISSUES``, so a problem a fixer
        # already deferred is not raised, and deferred, a second time under a
        # new finding id (PR #89 review F2, #90).
        self._existing_pr_follow_ups: list[tuple[str, str]] = []
        self.state: AutoForgeState | None = None
        # Set by locked(): the controller lock held for a whole command.
        self._lock: ControllerLock | None = None
        # Resolved by lock_path() on first use (never for a dry run): the lock
        # is keyed by the repository that contains workdir, not by state_dir.
        self._lock_path: Path | None = None
        # True while self.state is a snapshot of state.json taken by load();
        # such a snapshot is re-read under a self-acquired execution lock.
        self._state_from_disk = False
        # Where an agent launch's live progress lines go besides the step's
        # ``progress.log`` (#192): the CLI sets it to a stderr writer for a
        # real run. ``None`` (a library caller, a test, a dry run) prints
        # nothing; the log is written either way.
        self.progress_output: Callable[[str], None] | None = None

    # -- the state directory as a held capability --------------------------
    @property
    def paths(self) -> StatePaths:
        """Where this engine's run keeps its state (see :class:`StatePaths`)."""
        return self._paths

    @paths.setter
    def paths(self, value: StatePaths) -> None:
        # Re-pointing the engine at another location is a new binding, so a
        # capability held on the old one is released rather than carried.
        if self._state_root is not None:
            self._state_root.close()
            self._state_root = None
        self._paths = value

    def state_root(self, *, create: bool = True) -> SafeRoot:
        """The run's state directory as a capability (owned by the engine).

        Opened once -- the only pathname resolution of the state directory
        this engine ever performs -- and held thereafter. Every later call
        proves the pathname still names the held directory
        (:meth:`SafeRoot.verify_identity`) and returns the same descriptor,
        so a state directory replaced mid-run (by a symbolic link, a prepared
        ordinary directory, a rename) is a refusal and never a redirection:
        no write, no log and no read of ``state.json`` can reach the
        replacement, because nothing re-resolves the name into a binding.

        ``create=False`` raises ``FileNotFoundError`` when the directory does
        not exist (reads never create it).
        """
        if self._state_root is None:
            self._state_root = self._paths.open_root(create=create)
        else:
            self._state_root.verify_identity(role="state directory")
        return self._state_root

    def close(self) -> None:
        """Release the held state-directory capability (idempotent)."""
        if self._state_root is not None:
            self._state_root.close()
            self._state_root = None

    @property
    def github(self) -> GitHubClient:
        """The GitHub client, constructed on first use (never in LOCAL mode)."""
        if self._github is None:
            self._github = GitHubClient(
                gh_command=self.config.github.command,
                timeout_seconds=self.config.github.timeout_seconds,
            )
        return self._github

    @property
    def mode(self) -> WorkflowMode:
        """The workflow of the loaded run (REMOTE before any state exists)."""
        return self.state.mode if self.state is not None else WorkflowMode.REMOTE

    def workspace(self) -> LocalWorkspace:
        """The local working-tree reader (LOCAL mode's source of truth).

        Built from the *current* configuration: for a new run that is the
        definition being created, and for a loaded run it is what the
        contract gate (:meth:`local_contract`) compares against the
        persisted definition before any phase executes.
        """
        if self._workspace is None:
            local = self.config.local
            self._workspace = LocalWorkspace(
                workdir=self.workdir,
                runner=self._runner or execute,
                exclude=local.exclude,
                max_entries=local.max_workspace_entries,
                max_bytes=local.max_workspace_bytes,
            )
        return self._workspace

    # -- the LOCAL run contract ------------------------------------------
    def _invocation_contract(self) -> LocalRunContract:
        """What *this* invocation would define a LOCAL run as (never persisted twice)."""
        return LocalRunContract.from_config(self.config, self.workspace(), self.paths)

    def local_contract(self) -> LocalRunContract:
        """The loaded LOCAL run's persisted contract, proven to match this invocation.

        This is the one gate between "state was read" and "anything is
        executed or decided" (see :mod:`autoforge.run_contract`): it runs at
        :meth:`load`, at the top of every LOCAL step, and at every use of a
        run-defining value, because execution reads such values *from the
        contract this returns* rather than from the configuration. Drift is a
        VerificationError, not BLOCKED: the operator changed a setting (or
        moved the checkout), and both restoring it and starting a new run
        under the new one must stay possible. Nothing is persisted, so the
        run is exactly as resumable afterwards as it was before.
        """
        state = self._require_state()
        return validate_local_run_contract(state.local_contract(), self._invocation_contract())

    def _execution_cwd(self) -> str:
        """The directory agents and validation commands are launched from.

        For a LOCAL run this is the contract's ``repository_root``, never
        the invocation's cwd. A validation command is an argv, and an argv
        only means something relative to a directory: ``["./verify"]`` run
        from ``src/`` is a different program, and ``["pytest"]`` run from a
        subdirectory discovers a different rootdir and conftest. Freezing
        the argv while letting the directory float would let a ``resume``
        from a subdirectory silently redefine what verifies the run (round
        11, R11-F1). Taking the directory from the contract -- which the
        gate has already proven names this checkout -- is what makes the
        invocation's cwd genuinely DYNAMIC (see :mod:`autoforge.run_contract`):
        it decides where the repository is *found*, and nothing else.

        A REMOTE run has no contract; its agents are launched in the
        per-issue worktree (:meth:`_ensure_agent_worktree`), never in the
        checkout the controller is run from. GitHub, not a working-tree
        verifier, is its source of truth.
        """
        if self.mode is WorkflowMode.LOCAL:
            return self.local_contract().repository_root
        return str(self._ensure_agent_worktree())

    # -- REMOTE agent isolation ---------------------------------------------
    def _agent_worktree_name(self) -> str:
        """One worktree per issue: its number, or the EPIC's between issues."""
        s = self._require_state()
        if s.current_issue_url:
            return str(parse_issue_url(s.current_issue_url).number)
        return f"epic-{parse_issue_url(s.epic_url).number}"

    def agent_worktree_description(self) -> str:
        """The worktree path for messages and plans, without touching git.

        A dry run runs no ``git`` (it may be planned outside any repository),
        so until :meth:`agent_worktree_path` has resolved the path in this
        engine the default location is described rather than resolved.
        """
        name = self._agent_worktree_name()
        cached = self._agent_worktrees.get(name)
        if cached is not None:
            return str(cached)
        configured = self.config.execution.worktree_dir
        if configured:
            return str(Path(os.path.realpath(Path(self.workdir) / configured)) / name)
        return f"<git common dir>/autoforge/worktrees/{name}"

    def agent_worktree_path(self) -> Path:
        """Where this issue's agents work. Derives the path (one git read), creates nothing.

        Under ``execution.worktree_dir`` when configured (relative to the
        controller's working directory), otherwise under
        ``<git common dir>/autoforge/worktrees``, next to the controller lock
        and a LOCAL run's state: inside the repository's git directory,
        which is outside every working tree of the checkout, so the
        operator's tree never contains it and the agent's never contains
        ``.autoforge/``, the lock or the operator's uncommitted work. The
        common dir (not the per-worktree git dir) keys it, so a controller
        run from a linked worktree of the checkout uses the same place.
        """
        name = self._agent_worktree_name()
        cached = self._agent_worktrees.get(name)
        if cached is not None:
            return cached
        configured = self.config.execution.worktree_dir
        if configured:
            base = Path(os.path.realpath(Path(self.workdir) / configured))
        else:
            base = self.workspace().git_dirs()[-1] / "autoforge" / "worktrees"
        path = base / name
        self._agent_worktrees[name] = path
        return path

    def _worktree_identity(self, path: Path) -> tuple[Path, Path] | None:
        """``(working-tree root, git common dir)`` of ``path``, or ``None`` outside git.

        Both are needed to recognise a worktree the controller created: a
        plain directory under ``.git/`` answers ``--git-common-dir`` as the
        repository itself (git treats the inside of a git directory as that
        repository) but has no working tree, and a subdirectory of some
        worktree has that worktree's root, not its own.
        """
        res = (self._runner or execute)(
            local_git_request(
                ["-C", str(path), "rev-parse", "--show-toplevel", "--git-common-dir"],
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
        )
        if res.timed_out or res.truncated or res.exit_code != 0:
            return None
        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if len(lines) != 2:
            return None
        root = Path(os.path.realpath(lines[0]))
        common = Path(os.path.realpath(path / lines[1]))
        return root, common

    def _registered_worktree_roots(self) -> set[Path]:
        """The working-tree roots ``git worktree list`` registers for this repository.

        Read from the controller's own checkout, so the answer comes from
        the repository's registry (the common dir's ``worktrees/``), not
        from whatever the candidate path leads to. Git reports the paths
        resolved; a linked worktree whose tree is gone is listed as
        ``prunable`` and left out, since nothing at its path is a worktree.
        """
        res = (self._runner or execute)(
            local_git_request(
                ["worktree", "list", "--porcelain"],
                cwd=self.workdir,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
        )
        if res.timed_out or res.truncated or res.exit_code != 0:
            detail = (res.stderr or res.stdout).strip().splitlines()
            raise VerificationError(
                "cannot list the repository's worktrees (`git worktree list` "
                f"{'timed out' if res.timed_out else f'exited {res.exit_code}'}"
                f"{f': {detail[-1]}' if detail else ''}); the controller will not reuse "
                "an agent worktree it cannot confirm is registered"
            )
        roots: set[Path] = set()
        for entry in res.stdout.split("\n\n"):
            lines = [line.strip() for line in entry.splitlines() if line.strip()]
            if not lines or not lines[0].startswith("worktree "):
                continue
            if any(line == "prunable" or line.startswith("prunable ") for line in lines[1:]):
                continue
            roots.add(Path(os.path.realpath(lines[0][len("worktree ") :])))
        return roots

    def _ensure_agent_worktree(self) -> Path:
        """The issue's agent worktree, created on first use and never deleted.

        A new worktree is added detached at the checkout's current HEAD:
        the agent checks out what its phase needs (the prompts say so:
        ANALYZE_EXECUTE the start commit the controller fetched, staying
        detached, a later phase its PR branch), and no branch is created or
        moved here. An existing one is reused as the agent left it, with
        whatever it committed, which is what the next phase of the same
        issue wants. A path that exists but is not a worktree root of
        this repository (a stale directory, a foreign checkout, a
        subdirectory of some tree) is refused, not adopted: the controller
        never launches an agent somewhere it did not create. So is a
        symbolic link at the path, whatever it points to, decided on the
        entry itself before anything follows it; and reuse requires the
        directory to be registered in this repository's worktree list, not
        merely to answer git as some worktree. Nor is one
        created inside the operator's working tree (the git directory,
        which git never walks as content, excepted), where it would sit in
        their ``git status`` and the agent's tree would contain the
        operator's -- and, since ``mkdir`` and ``git worktree add`` would
        follow it there, nor at a path a symbolic link above it leads
        elsewhere: a worktree is created only at the literal derived path.
        Only :meth:`_invoke_phase` reaches this, so a dry run
        never creates one; the operator removes worktrees
        (``git worktree remove``), the controller does not.
        """
        path = self.agent_worktree_path()
        ws = self.workspace()
        common = ws.git_dirs()[-1]
        if path.is_symlink():
            # Decided on the entry itself (lstat), before anything follows the
            # link: `git -C` and realpath both answer for the *target*, so a
            # link to the operator's checkout or to another worktree of this
            # repository would otherwise pass every check below as that tree.
            target = os.readlink(path)
            raise VerificationError(
                f"the agent worktree path {path} is a symbolic link (to {target}), not a "
                f"worktree of the repository at {common}; the controller launches agents "
                "only in a directory it created itself, never through a link -- remove "
                "the link or configure execution.worktree_dir elsewhere"
            )
        if path.exists():
            found = self._worktree_identity(path)
            real = Path(os.path.realpath(path))
            if real != path:
                # `path` is not a link, so a component above it is: the base was
                # resolved when the path was derived and has changed since.
                what = f"reached through a symbolic link ({real})"
            elif found is None:
                what = "not a git worktree"
            elif found[1] != common:
                what = f"part of the repository at {found[1]}"
            elif found[0] != path:
                what = f"inside the worktree rooted at {found[0]}, not a worktree of its own"
            elif path == ws.root():
                what = "the checkout the controller is run from"
            elif path not in self._registered_worktree_roots():
                what = "not registered in the repository's worktree list"
            else:
                return path
            raise VerificationError(
                f"the agent worktree path {path} exists but is not a worktree of the "
                f"repository at {common} (it is {what}); move it aside or configure "
                "execution.worktree_dir elsewhere"
            )
        real = Path(os.path.realpath(path))
        if real != path:
            # The path does not exist, so a component above it is a link (the
            # base was resolved when the path was derived; `.git/autoforge` or
            # `worktrees` below it was not). `mkdir` and `git worktree add`
            # would follow it, and the guard below compares the literal path,
            # so a link into the operator's working tree would be followed
            # there unnoticed.
            raise VerificationError(
                f"cannot create the agent worktree {path}: it is reached through a "
                f"symbolic link ({real}); the controller creates a worktree only at the "
                "derived path itself, never through a link -- remove the link or "
                "configure execution.worktree_dir elsewhere"
            )
        operator_root = ws.root()
        inside_git_dir = any(d == path or d in path.parents for d in ws.git_dirs())
        if not inside_git_dir and (path == operator_root or operator_root in path.parents):
            raise VerificationError(
                f"cannot create the agent worktree {path} inside the checkout's working "
                f"tree {operator_root}; configure execution.worktree_dir outside it"
            )
        if not ws.head_sha():
            raise VerificationError(
                f"cannot create the agent worktree {path}: the checkout at "
                f"{Path(self.workdir).resolve()} has no commit yet, and a worktree needs a "
                "commit to start from"
            )
        path.parent.mkdir(parents=True, exist_ok=True)
        # Hardened like every controller git process (ADR 0004 D7.3, D7.5):
        # the checkout would otherwise run the repository's `post-checkout`
        # hook and its file-system monitor, both agent-writable, with the
        # controller's whole environment, and could check out a replacement
        # object's tree instead of HEAD's.
        res = (self._runner or execute)(
            local_git_request(
                ["worktree", "add", "--detach", str(path), "HEAD"],
                cwd=self.workdir,
                timeout_seconds=GIT_TIMEOUT_SECONDS,
            )
        )
        if res.timed_out or res.truncated or res.exit_code != 0:
            detail = (res.stderr or res.stdout).strip().splitlines()
            raise VerificationError(
                f"cannot create the agent worktree {path}: `git worktree add` "
                f"{'timed out' if res.timed_out else f'exited {res.exit_code}'}"
                f"{f' ({detail[-1]})' if detail else ''}"
            )
        return path

    def _checkout_anchor(self) -> tuple[str, str]:
        """HEAD and branch of the checkout the controller is run from."""
        ws = self.workspace()
        return ws.head_sha(), ws.branch()

    def _checkout_drift(self, anchor: tuple[str, str]) -> str:
        """How the operator's checkout moved since ``anchor`` was read, or ""."""
        head, branch = anchor
        now_head, now_branch = self._checkout_anchor()
        drift: list[str] = []
        if now_head != head:
            drift.append(
                f"HEAD moved from {head[:12] or '(unborn)'} to {now_head[:12] or '(unborn)'}"
            )
        if now_branch != branch:
            drift.append(
                f"the checked-out branch changed from {branch or '(detached HEAD)'} to "
                f"{now_branch or '(detached HEAD)'}"
            )
        return " and ".join(drift)

    def _invoke_phase_anchored(
        self, phase: Phase, reconcile: Callable[[], StepOutcome | None]
    ) -> dict | StepOutcome:
        """:meth:`_invoke_phase`, with the operator's checkout pinned around it.

        The LOCAL anchor check (:meth:`_git_anchor_drift`), reused for a
        REMOTE run: HEAD and the checked-out branch of the checkout the
        controller is run from are read before the launch and again after
        it returns -- however it returns -- and a change is a
        :class:`CheckoutDriftError`, which the step turns into BLOCKED. The
        agents are launched in the per-issue worktree and told to work only
        there, so the checkout moving means an agent (or someone else) did
        what the prompts forbid, and the controller cannot tell which;
        nothing is rolled back. The read itself failing is a
        VerificationError as in LOCAL mode: not evidence of no drift.
        """
        anchor = self._checkout_anchor()
        try:
            result = self._invoke_phase(phase, reconcile)
        except Exception as exc:
            drift = self._checkout_drift(anchor)
            if drift:
                outcome = f"The invocation itself ended with {type(exc).__name__}: {exc}"
                raise CheckoutDriftError(
                    self._checkout_drift_reason(phase, drift, outcome)
                ) from exc
            raise
        drift = self._checkout_drift(anchor)
        if drift:
            raise CheckoutDriftError(
                self._checkout_drift_reason(
                    phase,
                    drift,
                    "The agent returned; its claims were not verified and nothing was applied"
                    if isinstance(result, dict)
                    else "The phase was resolved from GitHub without applying the agent's claims",
                )
            )
        return result

    def _checkout_drift_reason(self, phase: Phase, drift: str, outcome: str) -> str:
        return (
            f"the checkout the controller is run from ({Path(self.workdir).resolve()}) moved "
            f"while {phase.value} ran: {drift}. Agents work only in the per-issue worktree "
            f"({self.agent_worktree_description()}); the controller never commits to, checks out, "
            "resets or switches branches in the checkout it is run from and requires the "
            "same of the agents it launches, because it cannot tell an agent's commit from "
            f"an operator's. Nothing was rolled back -- the controller never undoes a git "
            f"operation it did not perform. {outcome}. Inspect the checkout and the agent's "
            "GitHub work, then 'unblock'"
        )

    def _isolation_plan_notes(self) -> list[str]:
        """What a REMOTE agent step's isolation would be, for the plan / dry run.

        No git runs here: a dry run may be planned outside any repository.
        """
        names = self.config.execution.environment_names()
        return [
            f"agent working directory: {self.agent_worktree_description()} (a detached git "
            "worktree of this checkout, one per issue, created at the first agent launch "
            "for the issue and reused afterwards; the controller never deletes it)",
            f"agent environment: {len(names)} allow-listed name(s) from execution.env_allowlist "
            "plus the provider's own; nothing else is inherited",
            f"HEAD and branch of {Path(self.workdir).resolve()} are read before and after the "
            "invocation; a change enters BLOCKED",
        ]

    def bind_local_state_dir(self, explicit: str | Path | None = None) -> None:
        """Point :attr:`paths` at where this LOCAL run keeps its runtime state.

        See :func:`local_state_paths` for the rule. The resolved directory is
        checked against the working tree here rather than at first write: a
        state directory inside the reviewed tree is refused, never quietly
        excluded from the fingerprint.
        """
        ws = self.workspace()
        self.paths = local_state_paths(ws, explicit=explicit, configured=self.config.state_dir)
        ws.check_state_dir_location(self.paths.state_dir)

    def local_state_paths(self) -> StatePaths:
        """LOCAL mode's *default* state location, ignoring any configured one."""
        return local_state_paths(self.workspace())

    # -- state handling --------------------------------------------------
    def new_run(self, epic_url: str, issue_url: str) -> AutoForgeState:
        epic, issue = validate_epic_and_issue(epic_url, issue_url)
        now = utcnow_iso()
        self.state = AutoForgeState(
            run_id=generate_run_id(),
            repository=epic.repository,
            epic_url=epic.canonical,
            current_issue_url=issue.canonical,
            phase=Phase.INITIALIZING,
            created_at=now,
            updated_at=now,
            prompt_version=self.config.prompt_version or __prompt_version__,
        )
        self._state_from_disk = False
        return self.state

    def new_local_run(
        self, feature_spec_path: str | Path, allow_dirty: bool = False
    ) -> AutoForgeState:
        """Create a LOCAL run frozen to ``feature_spec_path``.

        Resolves and hashes the specification, records the repository's HEAD
        and the current working-tree state, and applies the dirty-tree policy
        (see :meth:`_check_baseline_clean`). Nothing is written to disk here;
        the caller persists the state under the controller lock.
        """
        ws = self.workspace()
        ws.check_state_dir_location(self.paths.state_dir)
        spec = read_feature_spec(ws, feature_spec_path)
        dirty = self._check_baseline_clean(ws.dirty_paths(), spec, allow_dirty)
        snapshot = ws.snapshot()
        # The one moment the current configuration *defines* the run. From
        # here on it is only ever compared against this.
        contract = self._invocation_contract()
        now = utcnow_iso()
        self.state = AutoForgeState(
            run_id=generate_run_id(),
            mode=WorkflowMode.LOCAL,
            phase=Phase.INITIALIZING,
            feature_spec_path=spec.relative_path,
            feature_spec_sha256=spec.sha256,
            base_head_sha=snapshot.head_sha,
            base_branch=snapshot.branch,
            local_run_contract=contract.to_dict(),
            workspace_fingerprint=snapshot.fingerprint,
            baseline_dirty_paths=dirty,
            created_at=now,
            updated_at=now,
            prompt_version=contract.prompt_version,
        )
        self._state_from_disk = False
        return self.state

    @staticmethod
    def _check_baseline_clean(
        dirty_paths: list[str], spec: FeatureSpec, allow_dirty: bool
    ) -> list[str]:
        """Dirty-working-tree policy for a new local run.

        v1 is deliberately strict: the tree must be clean apart from the
        feature specification itself (which is typically brand new and
        uncommitted). The controller does not try to tell an operator's
        unrelated in-progress edit apart from the implementation it is about
        to ask for — an agent may legitimately edit the very same file, so any
        such split would be a heuristic, and a wrong one would either hide
        real changes from the reviewer or attribute the operator's work to the
        feature.

        ``--allow-dirty`` does not make the controller smarter, it makes the
        situation *explicit*: the pre-existing paths are recorded in state,
        shown in ``status`` and named to the reviewer as pre-existing. They
        are never silently absorbed — and their *contents* are pinned, because
        the run's first fingerprint covers the whole working tree, dirty
        baseline included.

        The list comes from ``git status``, which is the operator's own notion
        of "work in the way". That is the right input for this policy question
        and the wrong one for identity, which is why the fingerprint is built
        from the controller's own walk instead.
        """
        dirty = [p for p in dirty_paths if p != spec.relative_path]
        if dirty and not allow_dirty:
            listed = ", ".join(dirty[:20]) + ("..." if len(dirty) > 20 else "")
            raise ConfigurationError(
                f"the working tree has {len(dirty)} change(s) besides the feature "
                f"specification: {listed}. A local run reviews the whole working tree, and "
                "the controller will not guess which changes belong to this feature. Commit "
                "or stash them first, or pass --allow-dirty to record them as pre-existing "
                "(they are then reported to you and to the reviewer, not hidden)."
            )
        return dirty

    def load(self) -> AutoForgeState:
        """Read the run at :attr:`paths` through the held state root.

        A LOCAL run is additionally passed through the contract gate here,
        so no command -- ``resume``, ``step``, ``status``, a crash recovery
        -- can act on a run whose definition this invocation would change.
        """
        try:
            root = self.state_root(create=False)
        except FileNotFoundError:
            # Reading never creates: no state directory means no run here.
            raise no_state_error(self.paths.state_file) from None
        state = load_state(self.paths.state_file, root=root)
        if state.is_local:
            # Gate *before* binding: an engine never holds a run whose
            # definition this invocation would change.
            validate_local_run_contract(state.local_contract(), self._invocation_contract())
        self.state = state
        self._state_from_disk = True
        return state

    def existing_run(self) -> AutoForgeState | None:
        """The run recorded at :attr:`paths`, or ``None`` when there is none.

        For the pre-flight of a *new* run: an unreadable or foreign state
        file is a StateError for the caller to refuse or quarantine, and a
        LOCAL run is *not* passed through the contract gate, because the
        question here is only whether something would be overwritten.
        """
        try:
            root = self.state_root(create=False)
        except FileNotFoundError:
            return None
        if root.lstat(STATE_FILENAME) is None:
            return None
        return load_state(self.paths.state_file, root=root)

    def quarantine_state(self) -> Path:
        """Move an unreadable ``state.json`` aside (see :func:`quarantine_state_file`)."""
        return quarantine_state_file(self.paths.state_file, root=self.state_root())

    def _require_state(self) -> AutoForgeState:
        if self.state is None:
            raise StateError(
                "no state loaded — call load() or new_run() first "
                "('resume' never creates a run silently)"
            )
        return self.state

    def save(self) -> None:
        """Persist the current state through the held state root."""
        assert self.state is not None
        self.state.settle_phase_effects()
        save_state(self.state, self.paths.state_file, root=self.state_root())

    _save = save

    def _record_verification_failure(self, phase: Phase, exc: VerificationError) -> None:
        """Persist a failed verification attempt (bounded, redacted).

        The message can quote untrusted text — a validation command's output,
        a PR body — and `state.json` is stored in the clear, so it passes
        through `redact` before it is persisted.
        """
        state = self._require_state()
        state.verification_failures.append(redact(f"{phase.value}: {exc}"))
        state.verification_failures = state.verification_failures[-20:]

    def _logger(self) -> RunLogger:
        state = self._require_state()
        return RunLogger(
            self.paths.logs_dir,
            state.run_id,
            # The logger closes what it is handed, so it gets its own handle
            # on the held (and just re-verified) directory.
            open_state_root=lambda: self.state_root().dup(),
        )

    def validate_config(self) -> None:
        """Fail early (ConfigurationError) when a required profile is unusable.

        A LOCAL run never enters REPLAN_REEXECUTE or UPDATE_EPIC, so it does
        not require those profiles to be configured. Which reviewer profiles it
        does require follows from `local.max_fix_rounds`, because local review
        rounds are routed exactly like remote ones.
        """
        required = (
            local_required_profiles(self.config)
            if self.mode == WorkflowMode.LOCAL
            else REQUIRED_PROFILES
        )
        validate_required_profiles(self.config, required)

    # -- prompt context ---------------------------------------------------
    @staticmethod
    def branch_name_for(issue_url: str) -> str:
        return f"autoforge/{parse_issue_url(issue_url).number}"

    @staticmethod
    def _format_follow_ups(pr_url: str, findings: list[dict], existing: dict[str, str]) -> str:
        """Per open finding: the follow-up issue that already exists, if any.

        Controller-rendered, not agent text: the finding ids passed the
        parser's shape check (``R<n>-F<m>``) and the URLs come from GitHub.
        No marker is rendered (#163): the controller writes every follow-up
        marker itself, so the fixer has none to copy.
        """
        if not pr_url or not findings:
            return "(none)"
        lines = []
        for f in findings:
            fid = str(f.get("id"))
            url = existing.get(fid, "(none)")
            lines.append(f"- {escape_inline(fid)}: existing issue: {escape_inline(url)}")
        return "\n".join(lines)

    @staticmethod
    def _format_existing_follow_ups(pairs: list[tuple[str, str]]) -> str:
        """The follow-up issues already open for this PR, one line per (finding, issue).

        Controller-rendered: the finding ids come from markers that passed
        the strict scan and the URLs from GitHub. An issue's title is
        untrusted text and is not quoted; the agent reads the issue itself.
        """
        if not pairs:
            return "(none)"
        return "\n".join(f"- {escape_inline(fid)}: {escape_inline(url)}" for fid, url in pairs)

    @staticmethod
    def _format_findings(findings: list[dict]) -> str:
        """The open findings as one untrusted block for a FIX prompt.

        Findings are reviewer output: evidence for the fixing agent to judge,
        never controller instructions. The whole list is quoted through the
        same unclosable fence as every other untrusted block, and each
        finding's one-line fields are kept to one line, so a title or a
        location cannot forge a second finding or a heading of its own.
        """
        if not findings:
            return fenced_untrusted_block("(none)", "text")
        lines = []
        for f in findings:
            fid = escape_inline(str(f.get("id")))
            head = f"- {fid} [{escape_inline(str(f.get('classification')))}]"
            loc = f.get("location") or ""
            title = f.get("title") or ""
            if loc:
                head += f" {escape_inline(str(loc))}"
            if title:
                head += f" — {escape_inline(str(title))}"
            lines.append(head)
            resolution = str(f.get("required_resolution", ""))
            lines.append("  Required resolution: " + resolution.replace("\n", "\n    "))
        return fenced_untrusted_block("\n".join(lines), "text")

    def _format_prior_findings(self) -> str:
        """The findings carried from a stale review, for the next REVIEW prompt.

        The provenance line is controller data (the round, the HEAD it was
        bound to and the comment the round was verified to have posted); the
        findings themselves are reviewer output and go through the same
        untrusted block as the FIX prompt's findings.
        """
        s = self._require_state()
        if not s.prior_findings:
            return "(none)"
        return (
            f"Review round {s.review_round} at HEAD `{s.reviewed_head_sha}` "
            f"({s.last_review_comment_url or 'comment URL not recorded'}) reported these "
            "findings, and no FIX round resolved them:\n" + self._format_findings(s.prior_findings)
        )

    def _format_previous_fix_resolutions(self) -> str:
        """The last FIX round's resolutions, for the next REVIEW prompt (#163, D5.3).

        The fixer no longer replies on the PR; its verified resolutions are
        handed to the reviewer as data instead. The finding ids, resolutions,
        commit SHAs and follow-up URLs are controller-verified, the rationales
        are the fixer's (redacted) text, so the whole list is quoted through
        the unclosable fence.
        """
        s = self._require_state()
        if not s.last_fix_resolutions:
            return "(none)"
        return (
            "The controller verified these against the commits it pushed and the follow-up "
            "issues it read back; each rationale is the fixer's text:\n"
            + fenced_untrusted_block(
                json.dumps(s.last_fix_resolutions, indent=2, ensure_ascii=False), "json"
            )
        )

    def _validation_commands_text(self) -> str:
        cmds = self.local_contract().validation_commands
        if not cmds:
            return "(none configured)"
        return "; ".join(" ".join(argv) for argv in cmds)

    def _local_prompt_variables(self) -> dict[str, str | int | None]:
        """Prompt context for a LOCAL phase: spec + working tree, no GitHub."""
        s = self._require_state()
        spec = self._frozen_spec()
        snapshot = self._local_snapshot_for_prompt()
        workspace_note = snapshot.describe()
        if s.baseline_dirty_paths:
            workspace_note += (
                " | pre-existing (NOT part of this feature, do not review or revert): "
                + ", ".join(escape_inline(path) for path in s.baseline_dirty_paths[:20])
            )
        return {
            "PROMPT_VERSION": s.prompt_version,
            "REPO_ROOT": escape_inline(str(self.workspace().root())),
            "FEATURE_SPEC_PATH": escape_inline(s.feature_spec_path),
            "FEATURE_SPEC_SHA256": s.feature_spec_sha256,
            "FEATURE_SPEC_BLOCK": fenced_untrusted_block(spec.content, "markdown"),
            "BASE_HEAD_SHA": s.base_head_sha or "(no commit yet)",
            "BASE_BRANCH": s.base_branch or "(detached HEAD)",
            "PRIOR_ATTEMPT": self._prior_attempt_note(),
            "WORKSPACE_FINGERPRINT": snapshot.fingerprint,
            "WORKSPACE_STATUS": workspace_note,
            "WORKSPACE_EXCLUSIONS": snapshot.describe_exclusions(),
            "VALIDATION_COMMANDS": self._validation_commands_text(),
            "REVIEW_ROUND": s.review_round + 1 if s.phase != Phase.FIX else s.review_round,
            "FINDINGS": self._format_findings(s.open_findings),
            **REVIEW_BOUND_VARIABLES,
            **FIX_BOUND_VARIABLES,
        }

    def _prior_attempt_note(self) -> str:
        """What to tell an agent whose phase was already invoked once."""
        s = self._require_state()
        if not self._local_resumed_invocation or s.local_pending_phase != s.phase.value:
            return "(none — this is the first attempt at this phase in this run)"
        return (
            f"A previous invocation of {s.phase.value} in this run was launched and never "
            "produced a result the controller could verify (it crashed, returned an invalid "
            "CONTROL_RESULT, or its validation commands failed). Work from that attempt may "
            "already be present in the working tree. Read what is there before you change "
            "anything, continue it rather than starting over, and do not revert or delete it "
            "just because you did not write it in this invocation."
        )

    def _frozen_spec(self) -> FeatureSpec:
        """Re-read the frozen specification, requiring its hash to be unchanged."""
        s = self._require_state()
        return verify_feature_spec_unchanged(
            self.workspace(),
            s.feature_spec_path,
            s.feature_spec_sha256,
            when=f"before {s.phase.value}",
        )

    def _local_snapshot_for_prompt(self) -> WorkspaceSnapshot:
        return self._local_bound_snapshot or self.workspace().snapshot()

    def _prompt_variables(self) -> dict[str, str | int | None]:
        s = self._require_state()
        upcoming_round = s.review_round + 1
        issue_number = parse_issue_url(s.current_issue_url).number if s.current_issue_url else 0
        reviewed_base = s.current_base_ref if s.phase == Phase.REVIEW else s.reviewed_base_ref
        reviewed_merge_base = (
            s.current_merge_base_sha if s.phase == Phase.REVIEW else s.reviewed_merge_base_sha
        )
        variables: dict[str, str | int | None] = {
            "PROMPT_VERSION": s.prompt_version,
            "REPOSITORY": s.repository,
            "EPIC_URL": s.epic_url,
            "ISSUE_URL": s.current_issue_url or "(none)",
            "ISSUE_NUMBER": issue_number,
            "PR_URL": s.current_pr_url or "(none)",
            "BRANCH": s.current_branch or self.branch_name_for(s.current_issue_url),
            "REVIEW_ROUND": upcoming_round if s.phase != Phase.FIX else s.review_round,
            "HEAD_SHA": s.current_head_sha or "(none)",
            "REVIEW_COMMENT_URL": s.last_review_comment_url or "(none)",
            "PREVIOUS_REVIEW_COMMENT_URL": s.last_review_comment_url or "(none)",
            "FOLLOW_UP_ISSUES": self._format_follow_ups(
                s.current_pr_url, s.open_findings, self._existing_follow_ups
            ),
            "EXISTING_FOLLOW_UP_ISSUES": self._format_existing_follow_ups(
                self._existing_pr_follow_ups
            ),
            "IMPLEMENTATION_MARKER": (
                render_implementation_marker(s.current_issue_url)
                if s.current_issue_url
                else "(none)"
            ),
            "REVIEWED_HEAD_SHA": (
                s.current_head_sha if s.phase == Phase.REVIEW else s.reviewed_head_sha
            )
            or "(none)",
            # The base the round is bound to, next to the HEAD, and the merge
            # base the reviewed diff is computed from (#96), a full SHA read
            # from GitHub.
            "REVIEWED_BASE_REF": escape_inline(reviewed_base or "(none)"),
            "REVIEWED_MERGE_BASE_SHA": reviewed_merge_base or "(none)",
            "FINDINGS": self._format_findings(s.open_findings),
            "PRIOR_FINDINGS": self._format_prior_findings(),
            "PREVIOUS_FIX_RESOLUTIONS": self._format_previous_fix_resolutions(),
            "MERGED_SINCE_EPIC_UPDATE": s.merged_since_epic_update,
            "EPIC_UPDATE_EVERY": self.config.workflow.epic_update_every,
            "MERGED_PRS_SINCE_EPIC_UPDATE": self._format_merged_since_epic_update(s),
            "ROADMAP_UPDATE_DUE": "yes" if self._roadmap_update_due(s) else "no",
            "ROADMAP_START_MARKER": ROADMAP_START_MARKER,
            "ROADMAP_END_MARKER": ROADMAP_END_MARKER,
            "MAX_ROADMAP_SECTION_CHARS": MAX_ROADMAP_SECTION_CHARS,
            "CURRENT_ROADMAP_SECTION": self._format_current_roadmap_section(),
            "LAST_REVIEW_RESULT": s.last_review_result or "(none)",
            "MAX_PROGRESS_CHARS": MAX_PROGRESS_CHARS,
            **REVIEW_BOUND_VARIABLES,
            **FIX_BOUND_VARIABLES,
        }
        if s.phase == Phase.ANALYZE_EXECUTE and s.mode == WorkflowMode.REMOTE:
            variables.update(self._analyze_prompt_variables())
        if s.phase == Phase.UPDATE_EPIC and s.mode == WorkflowMode.REMOTE:
            variables.update(self._update_epic_request_variables())
        if s.phase == Phase.REPLAN_REEXECUTE:
            # Before `_prepare_replan` has run (plan/dry-run rendering) the
            # checkpoint does not exist yet, so every field falls back to a
            # visible placeholder rather than to a plausible-looking guess.
            txn = ReplanTransaction.from_dict(s.replan_transaction)
            pending = "(checkpointed at execution)"
            variables.update(
                {
                    "PREVIOUS_PR_URL": txn.source_pr_url or s.current_pr_url,
                    "PREVIOUS_BRANCH": txn.source_branch or s.current_branch,
                    "PREVIOUS_HEAD_SHA": txn.source_head_sha or s.current_head_sha,
                    "DEFAULT_BRANCH": txn.base_branch or "(verified at execution)",
                    "ESCALATION_REASON": json.dumps(txn.escalation, sort_keys=True),
                    "EXECUTION_ATTEMPT": txn.expected_execution_attempt or s.execution_attempt + 1,
                    "HISTORICAL_FINDING_COUNT": txn.evidence_finding_count,
                    "HISTORICAL_FINDINGS": txn.rendered_findings or pending,
                    "HISTORICAL_OBSERVATIONS": txn.rendered_observations or pending,
                    "HISTORICAL_VERIFICATION_FAILURES": txn.rendered_verification_failures
                    or pending,
                    "REPLAN_TRANSACTION_ID": txn.transaction_id or "(generated at execution)",
                    "REPLAN_MARKER": (
                        txn.marker_example() if txn.transaction_id else "(generated at execution)"
                    ),
                }
            )
        return variables

    def _roadmap_update_due(self, s: AutoForgeState) -> bool:
        """Whether this UPDATE_EPIC entry writes the EPIC's roadmap section.

        The controller's batching decision (``workflow.epic_update_every``),
        read from persisted state: the merges counted since the last verified
        roadmap write. The agent is told the answer, never asked for it.
        """
        return s.merged_since_epic_update >= self.config.workflow.epic_update_every

    @staticmethod
    def _format_merged_since_epic_update(s: AutoForgeState) -> str:
        """The PRs merged since the last verified roadmap write, one per line.

        ``counted_merged_prs`` is appended in merge order and never truncated,
        and ``merged_since_epic_update`` counts its tail since the last
        ``record_epic_update``; the tail is the batch.
        """
        n = s.merged_since_epic_update
        prs = s.counted_merged_prs[-n:] if n > 0 else []
        if not prs:
            return "  (none)"
        return "\n".join(f"  - {escape_inline(url)}" for url in prs)

    def _update_epic_request_variables(self) -> dict[str, str | int | None]:
        """What a re-request prompt (``update_epic_rerequest.md``) names (D4.7, D13.7).

        Rendered for every UPDATE_EPIC prompt; only the re-request template
        uses them. The reason quotes a persisted rejection (redacted when it
        was persisted) and is escaped here, as every untrusted line is.
        """
        request = self._update_epic_request()
        context = self._update_epic_context()
        published = self._published_progress_comment_url()
        if request == UpdateEpicRequest.ROADMAP:
            asks = "the roadmap section"
        elif request == UpdateEpicRequest.SELECTION_WITH_ROADMAP:
            asks = "the next issue selection, with the roadmap section when one is required"
        else:
            asks = "the next issue selection"
        state = self._require_state()
        if context is not None and context.section_void:
            reason = (
                "the EPIC body changed outside the roadmap markers after the section was "
                "composed, so the controller did not write that section; compose it again "
                "from the current section below"
            )
        elif context is not None and context.selection_void and state.next_issue_rejections:
            reason = (
                "the controller rejected the previous selection: " + state.next_issue_rejections[-1]
            )
        elif context is None and self._adopted_progress_comment_url:
            reason = (
                "this run was upgraded while the phase was in progress: the progress comment "
                "above was posted by an agent under the previous contract, and the controller "
                "adopts it as this phase's publication"
            )
        else:
            reason = "(none)"
        fields = {
            UpdateEpicRequest.SELECTION: ['  "next_issue_url": "<next issue url or null>"'],
            UpdateEpicRequest.SELECTION_WITH_ROADMAP: [
                '  "roadmap_section": "<new content of the managed section, or null>",',
                '  "next_issue_url": "<next issue url or null>"',
            ],
            UpdateEpicRequest.ROADMAP: [
                '  "roadmap_section": "<new content of the managed section>"'
            ],
        }.get(request, [])
        return {
            "PROGRESS_COMMENT_URL": published or "(none)",
            "RE_REQUEST_ASKS": asks,
            "RE_REQUEST_REASON": escape_inline(reason),
            "RE_REQUEST_FIELDS": "\n".join(fields) or "(none)",
        }

    def _format_current_roadmap_section(self) -> str:
        split = self._epic_roadmap_at_entry
        if split is None:
            return "(read from the EPIC body immediately before the agent runs)"
        if split.section is None:
            return "(none: the EPIC has no managed section yet; the controller appends one)"
        return fenced_untrusted_block(split.section, "markdown")

    def template_for(self, phase: Phase) -> str | None:
        """The prompt template of ``phase`` in the loaded run's mode."""
        table = LOCAL_PHASE_TEMPLATE if self.mode == WorkflowMode.LOCAL else PHASE_TEMPLATE
        if self.mode == WorkflowMode.LOCAL and phase not in table:
            raise StateTransitionError(
                f"phase {phase.value} belongs to the REMOTE workflow and is never executed "
                "by a local run"
            )
        return table.get(phase)

    def _launch_template(self, phase: Phase) -> str | None:
        """The template the next launch of ``phase`` renders.

        ``template_for``, except for an UPDATE_EPIC whose progress comment is
        already published (ADR 0004 D4.7, D13.7): that launch asks only for
        the input the controller still needs.
        """
        template = self.template_for(phase)
        if (
            self.mode == WorkflowMode.REMOTE
            and phase == Phase.UPDATE_EPIC
            and self._update_epic_request() != UpdateEpicRequest.FULL
        ):
            return UPDATE_EPIC_RE_REQUEST_TEMPLATE
        return template

    def render_prompt_for(self, phase: Phase, correction_error: str | None = None) -> str:
        local = self.mode == WorkflowMode.LOCAL
        template = self._launch_template(phase)
        if template is None:
            raise StateTransitionError(f"phase {phase.value} has no agent prompt")
        if template not in TEMPLATE_FILES:
            raise StateTransitionError(f"unknown template {template}")
        variables = self._local_prompt_variables() if local else self._prompt_variables()
        prompt = render_phase(
            template,
            variables,
            common_template=LOCAL_COMMON_TEMPLATE if local else COMMON_TEMPLATE,
        )
        if correction_error is not None:
            correction = render(
                load_template("correction.md"),
                {"PREVIOUS_ERROR_BLOCK": fenced_untrusted_block(correction_error[:2000], "text")},
            )
            prompt = prompt + "\n\n---\n\n" + correction
        return prompt

    # -- planning (pure, no side effects) ----------------------------------
    def plan_step(self) -> StepPlan:
        s = self._require_state()
        if s.phase in TERMINAL_PHASES:
            raise StateTransitionError(f"phase {s.phase.value} is terminal — nothing to execute")
        if s.mode == WorkflowMode.LOCAL:
            return self._plan_local_step(s)
        if s.phase == Phase.INITIALIZING:
            return self._deterministic_plan(
                s,
                "INITIALIZING -> ANALYZE_EXECUTE (verify repository + issue via gh; no agent)",
                "ANALYZE_EXECUTE",
                notes=[
                    "would verify: cwd repo == issue repo, issue exists, issue is OPEN",
                    "would check for an existing open PR for the issue (recovery)",
                ],
            )
        if s.phase == Phase.READY_FOR_MERGE:
            return self._deterministic_plan(
                s,
                "READY_FOR_MERGE (holding state; automatic merge disabled)",
                "(holds unless merge gate is opened) | MERGE | REVIEW",
                notes=[
                    MERGE_GATE_MESSAGE,
                    "with the gate open, would verify via gh before entering MERGE: last "
                    "review clean and bound to this PR, PR open at the reviewed HEAD and "
                    "base, the recorded review comment re-read and still this round's "
                    "clean review of that revision, not draft, no change to "
                    "safety.protected_merge_paths, all checks succeeded, mergeable, no "
                    "auto-merge / merge queue (fail closed)",
                    *self._premerge_plan_notes(),
                ],
            )
        if s.phase == Phase.MERGE:
            return self._deterministic_plan(
                s,
                "MERGE (controller runs `gh pr merge` itself; no agent, no prompt)",
                "UPDATE_EPIC (only after gh reports the PR as MERGED)",
                notes=[
                    MERGE_GATE_MESSAGE,
                    "would verify: last review clean and bound to this PR, PR open at the "
                    "reviewed HEAD, base and merge base, the recorded review comment "
                    "re-read and still this round's clean review of that revision, not "
                    "draft, no change to safety.protected_merge_paths, all checks "
                    "succeeded, mergeable, no auto-merge / merge queue",
                    *self._premerge_plan_notes(),
                    "would run the command below (bound to the reviewed HEAD via "
                    "--match-head-commit), then re-read the PR and require state MERGED",
                ],
                command=self._merge_plan_command(),
            )
        notes: list[str] = []
        expected_next = self._expected_next(s.phase)
        budget = step_budget_reason(s.step_count, self.config.workflow.max_total_steps)
        if budget and not self._budget_may_stop(s.phase):
            notes.append(self._replan_finishes_under_budget_note(budget))
        elif budget:
            notes.append(f"would enter BLOCKED without executing: {budget}")
        if s.phase == Phase.ANALYZE_EXECUTE:
            notes.extend(self._analyze_plan_notes(s))
        if s.phase == Phase.UPDATE_EPIC:
            notes.extend(self._update_epic_plan_notes(s))
        if s.phase == Phase.REVIEW:
            cap = next_round_cap_reason(s.review_round, self.config.workflow.max_review_rounds)
            if cap:
                notes.append(f"would enter BLOCKED without invoking the reviewer: {cap}")
            notes.append(
                f"review round {s.review_round + 1} of at most "
                f"{self.config.workflow.max_review_rounds} (workflow.max_review_rounds)"
            )
            notes.extend(self._review_plan_notes(s))
        if s.phase == Phase.FIX:
            notes.extend(self._fix_plan_notes(s))
        if s.phase == Phase.REPLAN_REEXECUTE:
            replan_txn = ReplanTransaction.from_dict(s.replan_transaction)
            stage_notes, expected_next = self._replan_plan(replan_txn)
            notes.extend(stage_notes)
            if replan_txn.escalation:
                notes.append(f"replan policy: {json.dumps(replan_txn.escalation, sort_keys=True)}")
            notes.append(f"replan transaction stage: {replan_txn.stage.value}")
            if not may_invoke_agent(replan_txn):
                # The reducer resolves this step itself (`_drive_replan` never
                # falls through to the invocation past PREPARED), so the plan
                # carries no agent command, template or prompt: what it shows
                # is what the step can do (PR #92 review).
                return self._deterministic_plan(
                    s,
                    f"REPLAN_REEXECUTE at stage {replan_txn.stage.value} (controller finishes "
                    "the persisted transaction; no agent, no prompt)",
                    expected_next,
                    notes=notes,
                )
        profile = profile_for_phase(self.config, s.phase, s.review_round)
        variables = self._prompt_variables()
        prompt = self.render_prompt_for(s.phase)
        command = self.providers.get(profile).build_command_for(profile, prompt)
        notes.extend(self._isolation_plan_notes())
        return StepPlan(
            phase=s.phase.value,
            profile_name=profile.name,
            provider=profile.provider,
            model=profile.model,
            effort=profile.effort,
            command=command,
            limits=self.config.agent_limits(profile),
            loop_detection=self.config.execution.loop_detection,
            prompt_length=len(prompt),
            prompt_preview=prompt[:1200],
            prompt_full=prompt,
            routing=self._routing_info(s, profile.name),
            template=self._launch_template(s.phase) or "",
            review_round=s.review_round + 1 if s.phase == Phase.REVIEW else s.review_round,
            variables={k: str(v) for k, v in variables.items()},
            expected_next=expected_next,
            legal_next=self._legal_next_for(s.phase, s.mode),
            notes=notes,
        )

    def _plan_local_step(self, s: AutoForgeState) -> StepPlan:
        """Plan one LOCAL step. Pure: reads git and the spec, writes nothing.

        A dry run stops here, so this must never invoke an agent, run a
        validation command, write state, or touch GitHub.
        """
        if s.phase == Phase.INITIALIZING:
            plan = self._deterministic_plan(
                s,
                "INITIALIZING -> ANALYZE_EXECUTE (freeze the feature spec; no agent, no GitHub)",
                "ANALYZE_EXECUTE",
                notes=[
                    "mode: LOCAL (no gh, no PR, no push, no merge, no commits)",
                    f"feature specification: {s.feature_spec_path}",
                    f"frozen SHA-256: {s.feature_spec_sha256}",
                    "would verify: the specification is unchanged and the workspace is readable",
                ],
            )
            plan.variables = {
                "FEATURE_SPEC_PATH": s.feature_spec_path,
                "FEATURE_SPEC_SHA256": s.feature_spec_sha256,
                "BASE_HEAD_SHA": s.base_head_sha or "(no commit yet)",
                "BASE_BRANCH": s.base_branch or "(detached HEAD)",
                "WORKSPACE_FINGERPRINT": s.workspace_fingerprint,
            }
            return plan
        profile = profile_for_phase(self.config, s.phase, s.review_round)
        variables = self._local_prompt_variables()
        prompt = self.render_prompt_for(s.phase)
        command = self.providers.get(profile).build_command_for(profile, prompt)
        notes: list[str] = [
            "mode: LOCAL (no gh, no PR, no push, no merge)",
            f"feature specification: {s.feature_spec_path} "
            f"(frozen {s.feature_spec_sha256[:16]}...)",
            f"git anchor the run is pinned to: "
            f"{s.base_head_sha[:12] or '(unborn)'} on {s.base_branch or '(detached HEAD)'} "
            "(a commit, reset, checkout or branch switch enters BLOCKED)",
            f"workspace fingerprint now: {variables['WORKSPACE_FINGERPRINT']}",
        ]
        if s.local_pending_phase == s.phase.value:
            notes.append(
                f"a previous {s.phase.value} invocation was checkpointed and never verified "
                f"({s.local_pending_attempts} of {MAX_LOCAL_PHASE_ATTEMPTS} attempt(s) used); "
                "its work is credited against the fingerprint from before that attempt"
            )
        if s.baseline_dirty_paths:
            notes.append(
                "pre-existing working-tree changes recorded at run creation "
                f"(--allow-dirty): {', '.join(s.baseline_dirty_paths[:20])}"
            )
        contract = self.local_contract()
        budget = step_budget_reason(s.step_count, contract.max_total_steps)
        if budget:
            notes.append(f"would enter BLOCKED without executing: {budget}")
        if s.phase == Phase.REVIEW:
            cap = contract.max_review_rounds
            if s.review_round >= cap:
                notes.append(
                    "would enter BLOCKED without invoking the reviewer: local review round "
                    f"{s.review_round + 1} exceeds local.max_fix_rounds + 1 = {cap}"
                )
            notes.append(
                f"review round {s.review_round + 1} of at most {cap} "
                f"(local.max_fix_rounds={contract.max_fix_rounds})"
            )
            if variables["WORKSPACE_FINGERPRINT"] != s.workspace_fingerprint:
                notes.append(
                    "would enter BLOCKED without invoking the reviewer: the working tree "
                    f"changed since the controller last verified it (bound "
                    f"{s.workspace_fingerprint[:16]}...); REVIEW never re-binds"
                )
            notes.append(
                "the review is bound to the tree the controller last verified "
                f"({s.workspace_fingerprint[:16]}...); it is refused unless the tree still "
                "matches when the reviewer starts, and unless the reviewer reports exactly "
                "that value and leaves the tree unchanged"
            )
        else:
            notes.append(
                "would verify afterwards: the feature spec hash is unchanged, the workspace "
                "changed as claimed, and every configured validation command exits 0"
            )
        cmds = contract.validation_commands
        notes.append(
            "validation commands that would run after this phase: "
            + ("; ".join(" ".join(argv) for argv in cmds) if cmds else "(none configured)")
        )
        notes.append(
            "the agent and the validation commands run from the run's repository root "
            f"{contract.repository_root} (frozen; never the invocation's cwd)"
        )
        return StepPlan(
            phase=s.phase.value,
            profile_name=profile.name,
            provider=profile.provider,
            model=profile.model,
            effort=profile.effort,
            command=command,
            limits=self.config.agent_limits(profile),
            loop_detection=self.config.execution.loop_detection,
            prompt_length=len(prompt),
            prompt_preview=prompt[:1200],
            prompt_full=prompt,
            routing=self._routing_info(s, profile.name),
            template=self.template_for(s.phase) or "",
            review_round=s.review_round + 1 if s.phase == Phase.REVIEW else s.review_round,
            variables={k: str(v) for k, v in variables.items()},
            expected_next=self._local_expected_next(s.phase),
            legal_next=self._legal_next_for(s.phase, WorkflowMode.LOCAL),
            notes=notes,
        )

    @staticmethod
    def _local_expected_next(phase: Phase) -> str:
        return {
            Phase.INITIALIZING: "ANALYZE_EXECUTE",
            Phase.ANALYZE_EXECUTE: "REVIEW (after workspace + validation verification)",
            Phase.REVIEW: "DONE if clean, FIX if findings and the fix budget remains, "
            "BLOCKED if findings remain after it",
            Phase.FIX: "REVIEW (after workspace + validation verification)",
        }.get(phase, "")

    def _merge_argv(self) -> list[str]:
        """Exact ``gh`` argv the controller would run to merge the current PR."""
        s = self._require_state()
        return [
            self.config.github.command,
            *build_merge_argv(
                s.current_pr_url,
                method=self.config.merge.method,
                match_head_sha=(s.reviewed_head_sha or "").lower(),
                delete_branch=self.config.merge.delete_branch,
            ),
        ]

    def _merge_plan_command(self) -> list[str]:
        """Dry-run helper: the merge argv, or [] when state cannot yet produce one."""
        try:
            return self._merge_argv()
        except ConfigurationError:
            return []

    @staticmethod
    def _deterministic_plan(
        state: AutoForgeState,
        routing: str,
        expected: str,
        notes: list[str],
        command: list[str] | None = None,
    ) -> StepPlan:
        return StepPlan(
            phase=state.phase.value,
            profile_name="(none — deterministic transition)",
            provider="(none)",
            model="(none)",
            effort="(none)",
            command=list(command or []),
            limits=None,
            prompt_length=0,
            prompt_preview="(no agent prompt)",
            prompt_full="",
            routing=routing,
            template="",
            review_round=state.review_round,
            expected_next=expected,
            legal_next=ControllerEngine._legal_next_for(state.phase, state.mode),
            notes=notes,
        )

    @staticmethod
    def _expected_next(phase: Phase) -> str:
        return {
            Phase.ANALYZE_EXECUTE: (
                "REVIEW (after the controller pushes the verified HEAD, opens or adopts the "
                "PR, and reads it back via gh)"
            ),
            Phase.REVIEW: "FIX if any finding, READY_FOR_MERGE if clean, REVIEW if HEAD moved",
            Phase.FIX: (
                "REVIEW (after the controller creates the follow-up issues, appends the "
                "markers, pushes the verified HEAD and reads all of it back via gh)"
            ),
            Phase.REPLAN_REEXECUTE: "REVIEW (replacement PR; fresh review round 1)",
            Phase.MERGE: "UPDATE_EPIC (controller merge verified as MERGED via gh) | REVIEW",
            Phase.UPDATE_EPIC: "ANALYZE_EXECUTE | DONE",
        }.get(phase, "")

    @staticmethod
    def _legal_next_for(phase: Phase, mode: WorkflowMode = WorkflowMode.REMOTE) -> list[str]:
        return sorted(p.value for p in edges_for(mode).get(phase, frozenset()))

    def _next_phase(self, current: Phase, decision: dict) -> Phase:
        """The lifecycle edge out of ``current`` for the controller's ``decision``.

        Every transition that is not into a holding state is chosen by
        :func:`transitions.decide_next_phase`, the one decision function,
        from the verified result fields and the controller's own
        observations in ``decision``; the engine verifies and applies, it
        never picks a target phase itself. BLOCKED and FAILED are outside the
        topology and are entered by the engine directly, never through here.
        The chosen edge still goes through :func:`validate_transition` at the
        site that applies it.
        """
        return decide_next_phase(current, decision, self.mode)

    @staticmethod
    def _routing_info(state: AutoForgeState, profile_name: str) -> str:
        if state.phase == Phase.REVIEW:
            return f"review round {state.review_round + 1} -> profile {profile_name}"
        return f"{state.phase.value} -> profile {profile_name}"

    # -- public entry points (locking) --------------------------------------
    @property
    def lock_held(self) -> bool:
        """True while this engine holds the controller lock via :meth:`locked`."""
        return self._lock is not None

    def lock_path(self) -> Path:
        """The controller lock of the repository containing ``workdir``.

        Derived once from ``git rev-parse --git-common-dir`` (LockError when
        ``workdir`` is not inside a git repository) and cached, so every
        state directory, subdirectory and linked worktree of one repository
        contends for the same lock. Dry runs never call this.
        """
        if self._lock_path is None:
            self._lock_path = repository_lock_path(self.workdir)
        return self._lock_path

    @contextmanager
    def locked(self) -> Iterator[ControllerEngine]:
        """Hold the controller lock for a whole command lifecycle.

        A CLI command loads (or creates and saves) state and executes it
        inside one ``with engine.locked():`` block, so no other controller can
        replace ``state.json`` between the load and the execution. Inside the
        block :meth:`step` / :meth:`run` reuse the held lock instead of
        acquiring a second one (flock(2) is not reentrant across file
        descriptors; a nested acquisition would fail with LockError).
        The lock is released when the block exits, also on error.
        """
        if self._lock is not None:
            raise LockError(
                f"controller lock {self._lock.lock_path} is already held by this engine"
            )
        lock = ControllerLock(self.lock_path()).acquire()
        self._lock = lock
        try:
            yield self
        finally:
            self._lock = None
            lock.release()

    @contextmanager
    def _execution_lock(self) -> Iterator[None]:
        """Lock for a non-dry-run execution.

        Inside :meth:`locked` the already-held lock covers the execution.
        Otherwise the lock is acquired for this call only and, when the
        current state is a snapshot taken by :meth:`load`, the snapshot is
        discarded and ``state.json`` is re-read under the lock: a snapshot
        loaded before the lock may already have been replaced by another
        controller and must never be executed.
        """
        if self._lock is not None:
            yield
            return
        with ControllerLock(self.lock_path()):
            if self._state_from_disk:
                self.load()
            yield

    def step(self, dry_run: bool = False, allow_merge: bool = False) -> StepOutcome:
        if dry_run:
            return self._step_once(dry_run=True, allow_merge=allow_merge)
        with self._execution_lock():
            return self._step_once(dry_run=False, allow_merge=allow_merge)

    def run(
        self,
        max_steps: int = 50,
        dry_run: bool = False,
        allow_merge: bool = False,
        on_outcome: Callable[[StepOutcome], None] | None = None,
    ) -> list[StepOutcome]:
        """Loop ``step()`` until a stop phase or ``max_steps``.

        READY_FOR_MERGE is a stop phase only while the merge gate is closed.
        With the gate open (config AND ``allow_merge``) the loop continues
        through the controller-side pre-merge verification, MERGE and
        UPDATE_EPIC, so ``resume --allow-merge`` re-checks inconclusive
        GitHub data (bounded by ``merge.max_verification_attempts``).

        ``on_outcome`` is called with each step's outcome as soon as the
        step finished, before the next one starts (the CLI prints it, #192);
        the list returned is the same outcomes. It is not called for a dry
        run, whose plans are returned at once.
        """
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        if self.mode == WorkflowMode.LOCAL:
            # No READY_FOR_MERGE holding state exists locally.
            stop_phases = stop_phases_for(WorkflowMode.LOCAL)
        else:
            stop_phases = TERMINAL_PHASES if self.merge_gate_open(allow_merge) else STOP_PHASES
        if dry_run:
            outcomes = [self._step_once(dry_run=True, allow_merge=allow_merge)]
            assert self.state is not None
            if self.state.phase == Phase.INITIALIZING and max_steps > 1:
                saved_phase = self.state.phase
                self.state.phase = self._next_phase(Phase.INITIALIZING, {})
                try:
                    outcomes.append(self._step_once(dry_run=True, allow_merge=allow_merge))
                finally:
                    self.state.phase = saved_phase
            return outcomes
        all_outcomes: list[StepOutcome] = []
        with self._execution_lock():
            for _ in range(max_steps):
                assert self.state is not None
                if self.state.phase in stop_phases:
                    break
                outcome = self._step_once(dry_run=False, allow_merge=allow_merge)
                all_outcomes.append(outcome)
                if on_outcome is not None:
                    on_outcome(outcome)
        return all_outcomes

    # -- single step ---------------------------------------------------------
    def _outcome(
        self,
        previous: Phase,
        dry_run: bool = False,
        plan: StepPlan | None = None,
        result: dict | None = None,
        message: str = "",
    ) -> StepOutcome:
        state = self._require_state()
        return StepOutcome(
            run_id=state.run_id,
            previous_phase=previous.value,
            next_phase=state.phase.value if not dry_run else "(not advanced in dry-run)",
            dry_run=dry_run,
            plan=plan,
            result=result,
            message=message,
        )

    def _step_once(self, dry_run: bool, allow_merge: bool) -> StepOutcome:
        state = self._require_state()
        previous = state.phase
        if previous in TERMINAL_PHASES:
            if previous == Phase.DONE:
                return self._outcome(previous, dry_run, message="workflow already DONE")
            raise StateTransitionError(f"cannot step from terminal phase {previous.value}")

        plan = self.plan_step()
        if dry_run:
            return self._outcome(
                previous,
                dry_run=True,
                plan=plan,
                message=f"[dry-run] would execute {previous.value} via profile {plan.profile_name}",
            )

        if state.mode == WorkflowMode.LOCAL:
            # A LOCAL run's step budget is part of its contract, so it is
            # checked there, from the persisted value.
            return self._local_step_once(previous, plan)
        # Cumulative step budget: measured on persisted state, so `resume`
        # continues the same budget. Checked before anything executes -- except
        # that a replan journal past the write is finished first (#71).
        budget = step_budget_reason(state.step_count, self.config.workflow.max_total_steps)
        if budget and self._budget_may_stop(previous):
            return self._block(previous, plan, self._budget_block_reason(budget))
        if previous == Phase.REVIEW:
            # Review-round cap, whatever path led here (FIX, stale re-review,
            # HEAD drift from READY_FOR_MERGE/MERGE, resume): round cap+1 never
            # starts, no HEAD is bound and no reviewer is invoked.
            cap = next_round_cap_reason(state.review_round, self.config.workflow.max_review_rounds)
            if cap:
                return self._block(previous, plan, self._loop_block_reason(cap))

        # In memory until the step's first save: the launch checkpoint of an
        # agent step (`_invoke_phase`), or the resolution of a step that needs
        # none. An entry read that fails transiently before either persists
        # nothing, so it is not a step and is not charged: `resume` re-reads
        # with the same budget. The replan exemption above inherits this rule
        # rather than adding one.
        state.step_count += 1
        if previous == Phase.INITIALIZING:
            return self._initialize(plan)
        if previous == Phase.READY_FOR_MERGE:
            return self._ready_for_merge_step(plan, allow_merge)
        if previous == Phase.MERGE:
            return self._merge_step(plan, allow_merge)
        resolved = self._remote_entry(previous, plan)
        if resolved is not None:
            return resolved

        try:
            invoked = self._invoke_phase_anchored(
                previous, lambda: self._remote_entry(previous, plan)
            )
        except CheckoutDriftError as exc:
            # The operator's checkout moved under the agent (the prompts
            # forbid it; this is the controller enforcing it, as LOCAL does).
            return self._block(previous, plan, str(exc))
        if isinstance(invoked, StepOutcome):
            # A correction relaunch was pre-empted by the entry reconciliation:
            # the malformed attempt's GitHub work resolved the phase.
            return invoked
        payload = invoked
        status = payload.get("status")
        if status in ("failure", "blocked"):
            nxt = Phase.FAILED if status == "failure" else Phase.BLOCKED
            # Agent-supplied text, persisted in a plain `state.json` and
            # printed by the CLI: same redaction boundary as the run log, and
            # the same bound as every other block reason (#88); the parser
            # bounds this `message` only through the whole block's size.
            message = bound_block_reason(
                redact(str(payload.get("message", "") or f"agent reported {status}"))
            )
            state.phase = nxt
            state.block_reason = message
            self._save()
            return self._outcome(
                previous,
                plan=plan,
                result=payload,
                message=f"agent reported {status}: {message}",
            )

        try:
            nxt_phase, message = self._verify_and_apply(previous, payload)
        except (VerificationError, ControlResultValidationError) as exc:
            # The agent ran (and may have changed GitHub) but its claims did not
            # verify. Persist the attempt so `resume` re-enters this phase from
            # real state instead of pretending nothing happened.
            if isinstance(exc, VerificationError) and previous in (Phase.REVIEW, Phase.FIX):
                self._record_verification_failure(previous, exc)
            self._save()
            raise
        if nxt_phase == Phase.BLOCKED:
            # Verification gave up conclusively (bounded re-selection exhausted):
            # BLOCKED is an exceptional holding state outside the topology.
            return self._block(previous, plan, message)
        validate_transition(previous, nxt_phase)
        state.phase = nxt_phase
        state.attempt = 0
        self._save()
        return self._outcome(previous, plan=plan, result=payload, message=message)

    def _remote_entry(self, previous: Phase, plan: StepPlan) -> StepOutcome | None:
        """Reconcile a REMOTE phase entry with GitHub before its agent is launched.

        The controller cannot tell a first entry from a re-entry after an
        interrupted invocation (timeout, non-zero exit, malformed result,
        verification failure, refused run-log write, crash), and in every
        one of those the agent may already have done the phase's GitHub
        work. GitHub is the source of truth, so the entry reads it and either
        resolves the phase without an agent (returning the outcome), hands
        the existing write to the agent to adopt, or finds nothing and lets
        the launch proceed (returning ``None``).

        This runs before *every* launch of the phase's agent, not once per
        step: :meth:`_step_once` calls it before the first launch and
        :meth:`_invoke_phase` calls it again before each correction relaunch,
        because a correction is a re-entry too -- the agent whose result was
        malformed had every opportunity to post, push or create before it
        returned, and the correction prompt is advice to the agent, not a
        controller check. The probes are pure reads plus the persisted HEAD
        binding; none consumes a review round, a ``review_history`` entry or
        an attempt.
        """
        if previous == Phase.ANALYZE_EXECUTE:
            return self._reconcile_analyze_entry(plan)
        if previous == Phase.REVIEW:
            # Journal first: a saved plan is completed, never re-reviewed,
            # and its round stays bound to the revision its marker names.
            if isinstance(self._require_state().phase_effects().context, ReviewContext):
                return self._finish_review_entry(plan)
            self._bind_review_head()
            return self._reconcile_review_entry(plan)
        if previous == Phase.FIX:
            return self._prepare_fix(plan)
        if previous == Phase.REPLAN_REEXECUTE:
            # One reducer for the fresh step and for `resume`: it either
            # resolves the transaction (activated or refused) or falls through
            # to invoke the replacement agent.
            return self._drive_replan()
        if previous == Phase.UPDATE_EPIC:
            return self._reconcile_update_epic_entry(plan)
        return None

    # ======================================================================
    # LOCAL mode
    #
    # No GitHub client is ever constructed here. The working tree is the
    # source of truth, exactly as GitHub is in REMOTE mode: every agent claim
    # is re-derived from `git` by :mod:`autoforge.local_workspace` before it
    # is acted on, and the frozen feature specification is re-hashed around
    # every phase.
    def _local_step_once(self, previous: Phase, plan: StepPlan) -> StepOutcome:
        state = self._require_state()
        # The contract gate, before anything is counted, verified or run.
        contract = self.local_contract()
        # Cumulative step budget, from the contract: the bound the run was
        # created under, not today's `workflow.max_total_steps` (R9-F2 --
        # the gate above already refuses a changed one, and execution reads
        # run-defining values from the contract regardless).
        budget = step_budget_reason(state.step_count, contract.max_total_steps)
        if budget:
            return self._block(previous, plan, self._local_budget_block_reason(budget))
        if previous == Phase.INITIALIZING:
            state.step_count += 1
            return self._initialize_local(plan)
        if previous not in (Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.FIX):
            raise StateTransitionError(f"phase {previous.value} is not part of the LOCAL workflow")
        if previous == Phase.REVIEW:
            cap = contract.max_review_rounds
            if state.review_round >= cap:
                # Round cap+1 never starts: its findings could never be fixed.
                return self._block(
                    previous,
                    plan,
                    self._local_block_reason(
                        f"local review round {state.review_round + 1} would exceed the local "
                        f"bound of {cap} review round(s) "
                        f"(local.max_fix_rounds={contract.max_fix_rounds})"
                    ),
                )

        state.step_count += 1
        # Freeze check + git anchor + fingerprint binding, before the agent
        # runs. The bound status is what the prompt is rendered from, so the
        # fingerprint the reviewer is told to report is exactly the one
        # persisted here.
        self._frozen_spec()
        drift = self._git_anchor_drift()
        if drift:
            return self._block(previous, plan, self._local_anchor_block_reason(drift))
        before = self.workspace().snapshot()
        if previous == Phase.REVIEW:
            # REVIEW never binds a new fingerprint. `workspace_fingerprint` is
            # the tree as the controller last *verified* it -- the bytes the
            # validation commands passed on -- and a review is only ever bound
            # to that. A tree that differs here was written by something the
            # controller did not verify: a reviewer that edited the code and
            # then crashed (or returned, was refused, and is being resumed), a
            # process an agent left behind, or the operator. The controller
            # cannot tell those apart and does not try; re-binding would let
            # the next reviewer clear bytes no validation command ever saw
            # (R10-F1), so the run blocks and the tree is left as it is.
            if before.fingerprint != state.workspace_fingerprint:
                return self._block(previous, plan, self._local_unverified_tree_block_reason(before))
        else:
            state.workspace_fingerprint = before.fingerprint

        # Durable invocation checkpoint for the write-capable phases. It is
        # written by :meth:`_charge_local_launch` immediately before the agent
        # starts (inside ``_invoke_phase``), so a crash mid-invocation, a
        # malformed CONTROL_RESULT or a failing validation command all leave
        # the same recoverable record: "this phase was launched once and its
        # work may already be in the tree" -- and a refusal *before* the
        # launch (a corrupt event journal, an unusable profile, a missing
        # template) leaves no record, because nothing was launched (#57).
        # Here the entry only decides whether it is a fresh one or a retry
        # of a launched one, and whether the retry is still within bound.
        baseline = before.fingerprint
        if state.local_pending_phase and state.local_pending_phase != previous.value:
            # `load_state` refuses this shape; the check is repeated here so
            # that no in-memory path can resolve one phase and then close the
            # checkpoint another phase left behind.
            raise StateError(
                f"cannot enter {previous.value}: the LOCAL checkpoint records an "
                f"unverified {state.local_pending_phase} launch, which only "
                f"{state.local_pending_phase} may resume"
            )
        if previous in LOCAL_WRITE_PHASES:
            if state.local_pending_phase == previous.value:
                # An earlier attempt at this same phase entry never produced a
                # verified result. Its work counts, so "did anything actually
                # get implemented?" is judged against the fingerprint from
                # before *that* attempt, not against the tree it left behind.
                baseline = state.local_pending_fingerprint or before.fingerprint
                if state.local_pending_attempts >= MAX_LOCAL_PHASE_ATTEMPTS:
                    return self._block(
                        previous,
                        plan,
                        self._local_block_reason(
                            f"{previous.value} was invoked {state.local_pending_attempts} "
                            f"time(s) without ever producing a verified result, which is "
                            f"the bound of {MAX_LOCAL_PHASE_ATTEMPTS}. The controller will "
                            "not launch a write-capable agent at the same phase again"
                        ),
                    )
                self._local_resumed_invocation = True
        self._local_bound_snapshot = before
        self._save()
        try:
            payload = self._invoke_phase(previous)
        finally:
            self._local_bound_snapshot = None
            self._local_resumed_invocation = False

        status = payload.get("status")
        if status in ("failure", "blocked"):
            nxt = Phase.FAILED if status == "failure" else Phase.BLOCKED
            # Agent-supplied text, persisted in a plain `state.json` and
            # printed by the CLI: same redaction boundary as the run log, and
            # the same bound as every other block reason (#88); the parser
            # bounds this `message` only through the whole block's size.
            message = bound_block_reason(
                redact(str(payload.get("message", "") or f"agent reported {status}"))
            )
            state.phase = nxt
            state.block_reason = message
            self._clear_local_invocation()
            self._save()
            return self._outcome(
                previous,
                plan=plan,
                result=payload,
                message=f"agent reported {status}: {message}",
            )

        drift = self._git_anchor_drift()
        if drift:
            # The agent committed, reset or switched branches during its run.
            # The prompts forbid it; this is the controller enforcing it.
            return self._block(previous, plan, self._local_anchor_block_reason(drift))

        try:
            nxt_phase, message = self._verify_and_apply_local(previous, payload, before, baseline)
        except (VerificationError, ControlResultValidationError) as exc:
            # The agent ran and may have changed the working tree, but its
            # claims did not verify. Persist the attempt so `resume` re-enters
            # this phase from the real state of the tree — the invocation
            # checkpoint above is what keeps that re-entry from mistaking
            # already-produced work for work that was never done.
            if isinstance(exc, VerificationError):
                self._record_verification_failure(previous, exc)
            self._save()
            raise
        self._clear_local_invocation()
        if nxt_phase == Phase.BLOCKED:
            return self._block(previous, plan, message)
        validate_transition(previous, nxt_phase, WorkflowMode.LOCAL)
        state.phase = nxt_phase
        state.attempt = 0
        self._save()
        return self._outcome(previous, plan=plan, result=payload, message=message)

    def _charge_local_launch(self, phase: Phase) -> str:
        """Charge one write-capable launch of ``phase`` to the LOCAL checkpoint.

        Called immediately before *every* launch of a write-capable agent --
        the phase entry's first, a correction retry, a resumed attempt -- and
        nowhere else, so that what is charged is exactly what was launched.
        A refusal that lands earlier in ``_invoke_phase`` (the event journal,
        the profile, the prompt template) therefore charges nothing (#57).

        Returns ``""`` after charging, so the launch may proceed, or the
        reason it may not: the checkpoint has already spent
        :data:`MAX_LOCAL_PHASE_ATTEMPTS`. The charge is persisted by the
        caller's pre-launch save (:meth:`_invoke_phase`), together with the
        attempt counter, in the one write that precedes the agent: the
        persisted count is what bounds the phase entry, and a launch that was
        never charged is a launch a crash would let ``resume`` repeat. The
        first launch of an entry opens the checkpoint (phase, the fingerprint
        the entry was bound to, one attempt) in that same write; a checkpoint
        is never persisted with zero launches. A REMOTE run, or a read-only
        LOCAL phase, has no checkpoint to charge and is never refused here.
        """
        state = self._require_state()
        if state.mode != WorkflowMode.LOCAL or phase not in LOCAL_WRITE_PHASES:
            return ""
        if not state.local_pending_phase:
            if self._local_bound_snapshot is None:
                raise StateError(
                    f"cannot charge a {phase.value} launch: no workspace snapshot is bound"
                )
            state.local_pending_phase = phase.value
            state.local_pending_fingerprint = self._local_bound_snapshot.fingerprint
            state.local_pending_attempts = 0
        elif state.local_pending_phase != phase.value:
            raise StateError(
                f"cannot charge a {phase.value} launch: the LOCAL checkpoint records "
                f"{state.local_pending_phase}"
            )
        if state.local_pending_attempts >= MAX_LOCAL_PHASE_ATTEMPTS:
            return (
                f"{phase.value} has been launched {state.local_pending_attempts} time(s) "
                f"without ever producing a verified result, which is the bound of "
                f"{MAX_LOCAL_PHASE_ATTEMPTS}; the next step enters BLOCKED"
            )
        state.local_pending_attempts += 1
        return ""

    def _clear_local_invocation(self) -> None:
        """Close the pending-invocation checkpoint (the phase is resolved)."""
        state = self._require_state()
        state.local_pending_phase = ""
        state.local_pending_fingerprint = ""
        state.local_pending_attempts = 0

    def _git_anchor_drift(self) -> str:
        """Describe how HEAD/branch moved since the run started, or "".

        Two cheap `git` reads, deliberately not a full :meth:`status` call:
        this runs immediately before and after every agent phase, and hashing
        the tree to answer "did HEAD move?" would be wasteful.
        """
        state = self._require_state()
        ws = self.workspace()
        head, branch = ws.head_sha(), ws.branch()
        drift: list[str] = []
        if head != state.base_head_sha:
            drift.append(
                f"HEAD moved from {state.base_head_sha[:12] or '(unborn)'} to "
                f"{head[:12] or '(unborn)'}"
            )
        if branch != state.base_branch:
            drift.append(
                f"the checked-out branch changed from "
                f"{state.base_branch or '(detached HEAD)'} to {branch or '(detached HEAD)'}"
            )
        return " and ".join(drift)

    def _local_anchor_block_reason(self, drift: str) -> str:
        return self._local_block_reason(
            f"the repository's git anchor moved during the run: {drift}. A local run never "
            "commits, resets, checks out or switches branches, and it requires the same to "
            "be true of the agents it invokes: the accumulated findings and the frozen "
            "specification describe the working tree as it was anchored, and the controller "
            "cannot tell an agent's commit from an operator's. Nothing was rolled back — "
            "the controller never undoes a git operation it did not perform"
        )

    def _local_unverified_tree_block_reason(self, now: WorkspaceSnapshot) -> str:
        state = self._require_state()
        return self._local_block_reason(
            f"the working tree changed since the controller last verified it (fingerprint "
            f"{state.workspace_fingerprint[:16]}... -> {now.fingerprint[:16]}...; now "
            f"{now.describe()}). A review is bound only to a tree whose bytes passed the "
            "validation commands, and REVIEW never re-binds to a tree it did not verify: "
            "the change may be a reviewer's edit from an invocation that crashed or was "
            "refused, a process an agent left running, or an operator's, and the controller "
            "cannot tell those apart. Nothing was rolled back -- the controller never "
            "undoes a write it did not perform"
        )

    def _initialize_local(self, plan: StepPlan) -> StepOutcome:
        """Preflight for a local run: config, git, frozen spec. No GitHub."""
        state = self._require_state()
        self.local_contract()
        self.validate_config()
        ws = self.workspace()
        # Refuses a state directory inside the reviewed tree before any
        # fingerprint is computed (see check_state_dir_location). The contract
        # already proves the state directory is the one the run was created
        # with; this keeps the location rule itself in one place.
        ws.check_state_dir_location(self.paths.state_dir)
        spec = verify_feature_spec_unchanged(
            ws, state.feature_spec_path, state.feature_spec_sha256, when="before ANALYZE_EXECUTE"
        )
        drift = self._git_anchor_drift()
        if drift:
            return self._block(Phase.INITIALIZING, plan, self._local_anchor_block_reason(drift))
        snapshot = ws.snapshot()
        state.workspace_fingerprint = snapshot.fingerprint
        nxt = self._next_phase(Phase.INITIALIZING, {})
        validate_transition(Phase.INITIALIZING, nxt, WorkflowMode.LOCAL)
        state.phase = nxt
        self._save()
        return self._outcome(
            Phase.INITIALIZING,
            plan=plan,
            message=(
                f"froze feature specification {spec.relative_path} "
                f"(sha256 {spec.sha256[:16]}...) at {snapshot.anchor}; "
                f"INITIALIZING -> {nxt.value}"
            ),
        )

    def _local_block_reason(self, reason: str) -> str:
        state = self._require_state()
        return (
            f"{reason}. The run stays on feature {state.feature_spec_path}; the work is left "
            "in the working tree exactly as it is (nothing was committed, reverted or "
            "discarded). A human must inspect the open findings, or raise "
            "'local.max_fix_rounds' in the config and start a new run."
        )

    def _verify_and_apply_local(
        self, phase: Phase, payload: dict, before: WorkspaceSnapshot, baseline: str
    ) -> tuple[Phase, str]:
        """Verify one LOCAL agent result.

        ``before`` is the fingerprint from immediately before *this*
        invocation, which is what the agent's own ``changed_workspace`` claim
        describes. ``baseline`` is the fingerprint from before the *first*
        invocation of this phase entry; on a first attempt they are the same
        value, and on a retry ``baseline`` is what "an implementation exists"
        is judged against, so work an earlier attempt already produced is not
        demanded a second time.
        """
        if phase == Phase.ANALYZE_EXECUTE:
            return self._apply_local_analyze(
                LocalAnalyzeExecuteResult.from_payload(payload), before, baseline
            )
        if phase == Phase.REVIEW:
            return self._apply_local_review(LocalReviewResult.from_payload(payload))
        if phase == Phase.FIX:
            return self._apply_local_fix(LocalFixResult.from_payload(payload), before, baseline)
        raise StateTransitionError(f"phase {phase.value} does not accept local agent results")

    def _verify_workspace_change(
        self, phase: Phase, before: WorkspaceSnapshot, claimed: bool
    ) -> WorkspaceSnapshot:
        """Re-derive what actually changed and cross-check the agent's claim."""
        state = self._require_state()
        verify_feature_spec_unchanged(
            self.workspace(),
            state.feature_spec_path,
            state.feature_spec_sha256,
            when=f"after {phase.value}",
        )
        after = self.workspace().snapshot()
        changed = after.fingerprint != before.fingerprint
        if claimed != changed:
            raise VerificationError(
                f"{phase.value} reported changed_workspace={claimed} but the controller "
                f"observed the working tree {'change' if changed else 'stay identical'} "
                f"(fingerprint {before.fingerprint[:16]}... -> {after.fingerprint[:16]}...). "
                f"Current state: {after.describe()}"
            )
        return after

    def _apply_local_analyze(
        self, res: LocalAnalyzeExecuteResult, before: WorkspaceSnapshot, baseline: str
    ) -> tuple[Phase, str]:
        state = self._require_state()
        after = self._verify_workspace_change(Phase.ANALYZE_EXECUTE, before, res.changed_workspace)
        if after.fingerprint == baseline:
            raise VerificationError(
                "ANALYZE_EXECUTE reported success but the working tree is unchanged since "
                "this phase was first invoked: no implementation exists to review. An agent "
                "that cannot implement the feature must report status 'blocked' or 'failure' "
                "with a reason instead."
            )
        verified = self._verified_snapshot(Phase.ANALYZE_EXECUTE, after)
        state.workspace_fingerprint = verified.fingerprint
        state.last_review_result = ""
        tests = (
            f", tests attempted: {', '.join(res.tests_attempted)}" if res.tests_attempted else ""
        )
        nxt = self._next_phase(Phase.ANALYZE_EXECUTE, {})
        return nxt, (
            f"implementation verified: working tree now holds {verified.describe()}, "
            f"fingerprint {verified.fingerprint[:16]}...{tests}; "
            f"ANALYZE_EXECUTE -> {nxt.value}"
        )

    def _verified_snapshot(self, phase: Phase, after: WorkspaceSnapshot) -> WorkspaceSnapshot:
        """Run the validation commands and return the tree they left behind.

        ``after`` is the tree the agent produced. The commands are the
        controller's own, from the contract, and they may write into the tree
        (a test runner's cache, a build output), so the fingerprint the next
        REVIEW is bound to is taken *after* they ran: it is the tree as the
        controller's verification left it, and REVIEW refuses any other. When
        no command is configured nothing controller-side touched the tree
        and ``after`` is that fingerprint already; a second walk would only
        re-derive it.
        """
        if not self._run_validation_commands(phase):
            return after
        return self.workspace().snapshot()

    def _apply_local_review(self, res: LocalReviewResult) -> tuple[Phase, str]:
        state = self._require_state()
        expected_round = state.review_round + 1
        bound = state.workspace_fingerprint
        if res.round != expected_round:
            raise VerificationError(
                f"REVIEW round mismatch: expected {expected_round}, got {res.round}"
            )
        if res.reviewed_workspace_fingerprint != bound:
            raise VerificationError(
                f"REVIEW fingerprint mismatch: the controller bound workspace {bound}, "
                f"the agent reviewed {res.reviewed_workspace_fingerprint}. A review is only "
                "valid for the exact workspace it was bound to."
            )
        verify_feature_spec_unchanged(
            self.workspace(),
            state.feature_spec_path,
            state.feature_spec_sha256,
            when="after REVIEW",
        )
        after = self.workspace().snapshot()
        if after.fingerprint != bound:
            # REVIEW is read-only; a reviewer that edited the code changed the
            # very thing it was judging, so its verdict describes nothing that
            # still exists.
            raise VerificationError(
                f"the reviewer modified the working tree during REVIEW (fingerprint "
                f"{bound[:16]}... -> {after.fingerprint[:16]}...). A local review must not "
                f"change what it reviews. Current state: {after.describe()}"
            )

        # A valid review lifecycle: the round is consumed either way.
        state.review_round = res.round
        state.reviewed_workspace_fingerprint = bound
        state.last_review_needs_fix = res.needs_fix_round
        # Findings are agent-authored text persisted in plain `state.json` and
        # rendered into the next FIX prompt, so they cross the same redaction
        # boundary as the run log.
        findings = [redact_dict(f.to_dict()) for f in res.findings]
        # ``review_history`` is shared with REMOTE mode; locally the "reviewed
        # head" slot carries the workspace fingerprint, which plays exactly
        # the same role (what this verdict is valid for).
        self._record_review(
            res.round, bound, RESULT_NEEDS_FIX if res.needs_fix_round else RESULT_CLEAN, findings
        )
        nxt = self._next_phase(Phase.REVIEW, {"needs_fix_round": res.needs_fix_round})
        if res.needs_fix_round:
            state.last_review_result = "needs_fix"
            state.open_findings = findings
            budget = self.local_contract().max_fix_rounds
            if state.local_fix_rounds >= budget:
                return Phase.BLOCKED, self._local_block_reason(
                    f"local review round {res.round}: {len(findings)} finding(s) remain after "
                    f"{state.local_fix_rounds} fix round(s), which is the configured local "
                    f"budget (local.max_fix_rounds={budget})"
                )
            return nxt, (
                f"local review round {res.round}: {len(findings)} finding(s); REVIEW -> "
                f"{nxt.value} (fix round {state.local_fix_rounds + 1} of {budget})"
            )
        state.last_review_result = "clean"
        state.open_findings = []
        return nxt, (
            f"local review round {res.round} clean for workspace {bound[:16]}...; "
            f"REVIEW -> {nxt.value}. The implementation is in the working tree; committing "
            "it is yours to do."
        )

    def _apply_local_fix(
        self, res: LocalFixResult, before: WorkspaceSnapshot, baseline: str
    ) -> tuple[Phase, str]:
        state = self._require_state()
        open_ids = [f["id"] for f in state.open_findings]
        reported = [r.finding_id for r in res.resolutions]
        missing = sorted(set(open_ids) - set(reported))
        extra = sorted(set(reported) - set(open_ids))
        if missing or extra:
            raise VerificationError(
                f"FIX resolutions must cover exactly the open findings; missing={missing} "
                f"unknown={extra}"
            )
        after = self._verify_workspace_change(Phase.FIX, before, res.changed_workspace)
        fixed = [r for r in res.resolutions if r.resolution == "fixed"]
        if fixed and after.fingerprint == baseline:
            raise VerificationError(
                f"FIX claims {len(fixed)} 'fixed' resolution(s) but the working tree is "
                "unchanged since this fix round was first invoked; nothing was actually fixed."
            )
        state.last_fix_resolutions = [redact_dict(r.to_dict()) for r in res.resolutions]
        # The round number this attempt would conclude. `local_fix_rounds`
        # counts fix rounds the controller *concluded* — either back to REVIEW
        # or terminally BLOCKED — and deliberately not ones still in flight,
        # so it is charged at each of those two exits rather than here. A FIX
        # whose validation command fails is neither: the phase stays at FIX
        # for `resume`, and charging the budget for an attempt no controller
        # verified would spend a fix round on work that was never accepted and
        # block a later round that would have been. Re-entry stays bounded by
        # the separate `local_pending_attempts` checkpoint, which counts
        # *invocations* and is what stops a phase that can never be completed.
        round_no = state.local_fix_rounds + 1

        # An 'unresolved' disposition is the agent saying the finding is real
        # and it could not resolve it. That is an agent-reported blocker, and
        # the controller must not let it evaporate: clearing `open_findings`
        # here would let the next reviewer return a clean verdict and carry
        # the run to DONE with an acknowledged, unaddressed finding in it.
        # There is no "re-review it and see" policy to fall back on — the
        # reviewer is a different profile with no memory of the disposition —
        # so this is terminal and a human decides.
        unresolved = [r for r in res.resolutions if not r.is_resolved]
        if unresolved:
            keep = {r.finding_id for r in unresolved}
            state.open_findings = [f for f in state.open_findings if f.get("id") in keep]
            state.last_review_result = "unresolved"
            # Concluded, terminally: the round is charged.
            state.local_fix_rounds = round_no
            detail = "; ".join(
                f"{r.finding_id}: {redact(r.rationale) or '(no rationale)'}" for r in unresolved
            )
            return Phase.BLOCKED, self._local_block_reason(
                f"local fix round {round_no} left {len(unresolved)} of "
                f"{len(res.resolutions)} finding(s) explicitly unresolved, so the "
                f"implementation is not complete and no later review can clear them "
                f"({detail}). The validation commands were not run"
            )

        verified = self._verified_snapshot(Phase.FIX, after)
        state.workspace_fingerprint = verified.fingerprint
        state.local_fix_rounds = round_no
        state.open_findings = []
        state.last_review_result = "fixed"
        no_change = [r.finding_id for r in res.resolutions if r.resolution != "fixed"]
        note = (
            f", {len(no_change)} resolved without a change ({', '.join(no_change)})"
            if no_change
            else ""
        )
        nxt = self._next_phase(Phase.FIX, {})
        return nxt, (
            f"local fix round {round_no} verified: {len(res.resolutions)} "
            f"resolution(s){note}; FIX -> {nxt.value} (round {state.review_round + 1})"
        )

    def _run_validation_commands(self, phase: Phase) -> bool:
        """Run ``local.validation_commands`` and require every one to exit 0.

        Controller-owned verification, not something an agent reports: the
        commands come from configuration as argv arrays and are executed
        through the normal executor, never through a shell. A non-zero exit
        means the phase is not verified, so the run stays in this phase for
        ``resume`` rather than advancing on an agent's word. Returns whether
        any command ran (``False`` when none is configured).
        """
        state = self._require_state()
        # From the contract, not the configuration: what verifies this run
        # was decided when the run was created.
        commands = self.local_contract().validation_commands
        if not commands:
            return False
        # Likewise the directory they run *from*: an argv is only a program
        # relative to one (see :meth:`_execution_cwd`).
        cwd = self._execution_cwd()
        logger = self._logger()
        for argv in commands:
            # The commands are the repository's (a Makefile target, a test
            # runner the tree defines): allow-listed environment, as for an
            # agent, not the operator's whole shell.
            req = ExecutionRequest(
                command=list(argv),
                cwd=cwd,
                timeout_seconds=self.config.execution.command_timeout_seconds,
                env_allowlist=self.config.execution.environment_names(),
                contain_orphans=True,
            )
            result = (self._runner or execute)(req)
            record = ExecutionRecord(
                run_id=state.run_id,
                seq=0,
                phase=f"{phase.value}-validation",
                attempt=state.attempt,
                review_round=state.review_round,
                profile="(controller validation command)",
                prompt_version=state.prompt_version,
                command=list(argv),
                cwd=cwd,
                timeout_seconds=req.timeout_seconds,
                started_at=result.started_at,
                finished_at=result.finished_at,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                stdout_truncated=result.stdout_truncated,
                stderr_truncated=result.stderr_truncated,
                descendants_killed=result.descendants_killed,
                group_survived_kill=result.group_survived_kill,
                capture_abandoned=result.capture_abandoned,
                orphans_killed=result.orphans_killed,
                orphan_survived_kill=result.orphan_survived_kill,
                orphans_unchecked=result.orphans_unchecked,
                metadata={"validation_command": list(argv), "feature": state.feature_spec_path},
            )
            if result.timed_out or result.exit_code != 0:
                record.error = _with_leftovers(
                    f"timed out after {req.timeout_seconds}s"
                    if result.timed_out
                    else f"exit {result.exit_code}",
                    result.leftovers,
                )
            logger.log_execution(record, "", result.stdout or "", result.stderr or "")
            # Both the command line and its output can carry a secret into
            # `state.verification_failures` (plain `state.json`) through the
            # raised message, so both are redacted here and not only on the
            # run-log path. Baseline protection: `redaction` claims no
            # completeness.
            shown = " ".join(redact_argv(list(argv)))
            if result.timed_out:
                raise VerificationError(
                    _with_leftovers(
                        f"validation command {shown!r} timed out after {req.timeout_seconds}s",
                        result.leftovers,
                    )
                    + f"; {phase.value} is not verified."
                )
            if result.exit_code != 0:
                tail = redact((result.stderr or result.stdout or "").strip())[-2000:]
                raise VerificationError(
                    _with_leftovers(
                        f"validation command {shown!r} failed with exit {result.exit_code}",
                        result.leftovers,
                    )
                    + f"; {phase.value} is not verified and the run stays in "
                    f"{phase.value}. Output tail: {tail}"
                )
        return True

    # -- deterministic steps ---------------------------------------------------
    def _initialize(self, plan: StepPlan) -> StepOutcome:
        """Preflight: repository/issue verification, then -> ANALYZE_EXECUTE."""
        state = self._require_state()
        self.validate_config()
        repo = self.github.current_repo()
        if repo.name_with_owner.lower() != state.repository.lower():
            raise VerificationError(
                f"repository mismatch: cwd resolves to {repo.name_with_owner!r} via gh, "
                f"but the issue/epic belong to {state.repository!r}"
            )
        issue = self._verify_issue_selectable(state.current_issue_url, switching=False)
        nxt = self._next_phase(Phase.INITIALIZING, {})
        validate_transition(Phase.INITIALIZING, nxt)
        state.phase = nxt
        state.current_branch = ""
        self._save()
        return self._outcome(
            Phase.INITIALIZING,
            plan=plan,
            message=(
                f"verified issue #{issue.number} ({issue.state}); INITIALIZING -> {nxt.value}"
            ),
        )

    def _verify_issue_selectable(self, url: str, *, switching: bool) -> IssueInfo:
        """Verify on GitHub that ``url`` is an issue this run may work on.

        Shared by INITIALIZING (the first issue) and UPDATE_EPIC (the agent's
        ``next_issue_url``, untrusted). The issue must parse as a GitHub issue
        URL, belong to the run's repository, not be the EPIC itself, not be the
        current issue when ``switching`` (UPDATE_EPIC must not loop back onto the
        issue just finished), exist on GitHub and be OPEN. Any of those failing
        raises :class:`VerificationError` — a problem with the *selection*. No
        GitHub read is made for a URL that already fails the local checks (a
        foreign repository is never queried).

        Identity (EPIC / current issue) is compared with
        :meth:`GitHubRef.same_target`, never by URL string: owner and repository
        names are case-insensitive on GitHub, so a casing variant of the EPIC
        URL is still the EPIC.

        A GitHub read that fails for a reason unrelated to the selection is not
        turned into a VerificationError: :class:`GitHubUnavailableError`
        (transient) and every other conclusive :class:`GitHubError`
        (authentication, permissions, malformed data) propagate unchanged so
        the caller can apply its own retry / block policy. Only
        :class:`GitHubNotFoundError` is about the selection ("no such issue").
        """
        state = self._require_state()
        what = "next issue" if switching else "issue"
        try:
            ref = parse_issue_url(url)
        except ConfigurationError as exc:
            raise VerificationError(f"{what} {url!r} is not a GitHub issue URL: {exc}") from exc
        if not ref.same_repository(state.repository):
            raise VerificationError(
                f"{what} {ref.canonical} belongs to {ref.repository!r}, not {state.repository!r}"
            )
        if ref.same_target(parse_issue_url(state.epic_url)):
            raise VerificationError(f"{what} {ref.canonical} is the EPIC itself")
        if switching and ref.same_target(parse_issue_url(state.current_issue_url)):
            raise VerificationError(f"{what} {ref.canonical} is the issue that was just finished")
        try:
            issue = self.github.get_issue(ref.canonical)
        except GitHubNotFoundError as exc:
            raise VerificationError(
                f"{what} {ref.canonical} does not exist on GitHub "
                f"(or is not visible to this token): {exc}"
            ) from exc
        if issue.repository and issue.repository.lower() != state.repository.lower():
            raise VerificationError(
                f"{what} {ref.canonical} belongs to {issue.repository!r}, not {state.repository!r}"
            )
        if not issue.is_open:
            raise VerificationError(
                f"{what} {ref.canonical} is {issue.state}; only OPEN issues can be run"
            )
        return issue

    def _recover_pr(self, pr: PRInfo, plan: StepPlan | None) -> StepOutcome:
        """Bind the issue's open implementation PR and enter REVIEW without an agent.

        The idempotency guard of a *fresh* ANALYZE_EXECUTE entry (see
        :meth:`_reconcile_analyze_entry`): the issue's implementation PR is
        the open PR carrying the ``ai-implementation`` marker for it
        (:func:`render_implementation_marker`), found in a complete listing
        of the repository's open PRs, read to the end (issue #20). The
        marker is the identity the controller's own read-back requires of
        the PR it opens or adopts, so a PR that read-back would accept is a
        PR every later entry finds, whatever its branch is called. A PR the
        controller already persisted is a candidate as well, marker or not:
        it is the controller's own verified record.
        """
        state = self._require_state()
        url = parse_pr_url(pr.url).canonical
        state.current_pr_url = url
        state.current_head_sha = pr.head_sha
        state.current_base_ref = pr.base_ref
        state.current_merge_base_sha = ""  # bound at REVIEW entry
        state.current_branch = pr.head_ref
        nxt = self._next_phase(Phase.ANALYZE_EXECUTE, {})
        validate_transition(Phase.ANALYZE_EXECUTE, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.ANALYZE_EXECUTE,
            plan=plan,
            message=(
                f"recovered existing open PR {url} (HEAD {pr.head_sha[:12]}, "
                f"branch {pr.head_ref}); ANALYZE_EXECUTE -> {nxt.value} without invoking "
                "the agent"
            ),
        )

    def _implementation_pr_candidate(self) -> tuple[PRInfo | None, str]:
        """The one open PR that implements the current issue, if it is knowable.

        The probe behind :meth:`_reconcile_analyze_entry` (and behind
        ``unblock`` for a run with no PR bound): the persisted PR if it is still open, plus
        the open PR carrying the issue's ``ai-implementation`` marker from a
        complete listing. Returns ``(pr, "")`` for exactly one usable
        candidate, ``(None, "")`` for none, and ``(None, reason)`` when the
        answer is not knowable -- an unreadable persisted PR, a listing that
        cannot be read to the end, a marker defect, two candidates, a PR of
        another repository, one headed in a fork (never adopted, ADR 0004
        D9.4) or one without a readable HEAD. ``reason`` is the
        text the caller blocks (or refuses) with; the controller never
        guesses. A transient GitHub failure propagates unchanged.
        """
        state = self._require_state()
        issue = parse_issue_url(state.current_issue_url)
        candidates: dict[str, PRInfo] = {}
        if state.current_pr_url:
            try:
                pr = self.github.get_pr(state.current_pr_url)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return None, (
                    f"state references PR {state.current_pr_url} but it cannot be read: {exc}"
                )
            if pr.is_open:
                candidates[parse_pr_url(pr.url).canonical] = pr
        try:
            holder = self._implementation_prs(issue).at_most_one()
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return None, (
                f"cannot establish whether an open PR already implements issue "
                f"#{issue.number}: {exc}. The controller will not launch an agent that "
                "could create a second one"
            )
        except ClaimConflictError as exc:
            return None, (
                f"{exc}. The controller will not launch an agent that could create a second "
                "implementation and never guesses which PR is the issue's: close the stale "
                "PR(s) or repair the unreadable marker, then resume"
            )
        if holder is not None:
            candidates.setdefault(parse_pr_url(holder.obj.url).canonical, holder.obj)
        if not candidates:
            return None, ""
        if len(candidates) > 1:
            return None, (
                f"{len(candidates)} open PRs claim to implement issue #{issue.number}: "
                f"{', '.join(sorted(candidates))}. Close the stale ones and resume; the "
                "controller never guesses."
            )
        url, pr = next(iter(candidates.items()))
        if parse_pr_url(url).repository.lower() != state.repository.lower():
            return None, f"open PR {url} is not in repository {state.repository}"
        if pr.head_repository and pr.head_repository.lower() != state.repository.lower():
            return None, (
                f"open PR {url} carries the implementation marker for issue #{issue.number} "
                f"but is headed in the fork {pr.head_repository}: a fork-head PR is never "
                "adopted (ADR 0004 D9.4), because the controller cannot publish to its "
                "branch. Close it or remove its marker, then 'unblock'"
            )
        if not pr.head_sha:
            return None, f"open PR {url} has no readable head SHA; cannot recover"
        return pr, ""

    # -- ANALYZE_EXECUTE: the controller publishes the candidate (#161) ---------
    def _reconcile_analyze_entry(self, plan: StepPlan) -> StepOutcome | None:
        """Journal first, then GitHub, before any ANALYZE_EXECUTE launch (ADR 0004 D9.4).

        The controller publishes this phase itself: it pushes the agent's
        commit to ``autoforge/<n>`` (K1), then opens the implementation PR
        (K2), or adopts an open PR already on that branch (K3). So:

        - A persisted completion context means the agent's commit was
          accepted and the plan saved. The phase is completed from the
          journal and the agent is never launched for it again.
        - Otherwise an open PR carrying the issue's implementation marker is
          recovered at a fresh entry (bound, then REVIEW, no agent). After a
          launch of this entry it is not: the entry observation recorded
          none, so the controller did not open it, and it blocks rather
          than being adopted. The one exception is the legacy re-entry of a
          run upgraded while an agent of the previous contract was
          publishing (D13.4): it has no observation and recovers as before.
        - The default branch head (the candidate's base), the head of
          ``autoforge/<n>`` and the PRs headed at it are read, and both heads
          fetched into the shared object store, before the launch. A branch
          with no PR is earlier work: the agent continues from its head and
          the push is a fast-forward over it. An open, unmarked PR on it is
          the PR the controller adopts. A closed or merged PR on the branch,
          or a branch head the observation does not explain, blocks.
        - The observation records the default branch, its head, the branch
          head and the open PR on the branch (or none), and a re-entry it
          honors finds all of them again or blocks: a PR that appeared on,
          left or was replaced on the branch, or a default branch renamed
          since, is never adopted or targeted from a stale read (K2, K3).

        Only an unavailable GitHub and a failed fetch propagate (nothing was
        launched and 'resume' reads again); a conclusive failure blocks.
        """
        state = self._require_state()
        self._analyze_entry = None
        self._analyze_candidate = ""
        effects = state.phase_effects()
        if isinstance(effects.context, AnalyzeContext):
            return self._finish_analyze_entry(plan)
        pr, problem = self._implementation_pr_candidate()
        if problem:
            return self._block(Phase.ANALYZE_EXECUTE, plan, problem)
        observation = effects.observation
        honored = (
            observation
            if observation is not None
            and state.attempt >= 1
            and not is_legacy_reentry(Phase.ANALYZE_EXECUTE, state.attempt, state.launch_label)
            else None
        )
        if pr is not None:
            if honored is not None:
                return self._block(Phase.ANALYZE_EXECUTE, plan, self._unjournaled_pr_text(pr.url))
            return self._recover_pr(pr, plan)
        branch = self.branch_name_for(state.current_issue_url)
        ref = f"refs/heads/{branch}"
        try:
            default_branch = self.github.get_repo(state.repository).default_branch
            if not default_branch or default_branch == branch:
                return self._block(
                    Phase.ANALYZE_EXECUTE,
                    plan,
                    f"GitHub names {default_branch!r} as the default branch of "
                    f"{state.repository}; the controller publishes issue work to {branch!r} "
                    "and never to the default branch. Nothing was launched",
                )
            if honored is not None:
                recorded_base = [r for r in honored.refs if r != ref]
                if (
                    honored.base_sha is None
                    or len(recorded_base) != 1
                    or honored.refs[recorded_base[0]] != honored.base_sha
                ):
                    raise StateError(
                        "the ANALYZE_EXECUTE entry observation records no default branch head"
                    )
                if recorded_base[0] != f"refs/heads/{default_branch}":
                    return self._block(
                        Phase.ANALYZE_EXECUTE,
                        plan,
                        self._default_branch_changed_text(
                            recorded_base[0].removeprefix("refs/heads/"), default_branch
                        )
                        + ". Nothing was launched, pushed or created. 'unblock' starts a fresh "
                        "entry that reads the default branch again",
                    )
                base_sha = honored.base_sha
            else:
                base_sha = self.github.get_branch_head_sha(state.repository, default_branch)
            remote_head = self._branch_head(branch)
            adopt, problem = self._analyze_branch_pr(branch, default_branch)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.ANALYZE_EXECUTE,
                plan,
                f"the default branch, branch {branch!r} or the PRs headed at it could not be "
                f"read: {exc}. This is not a transient GitHub failure (authentication, "
                "permissions, or malformed data), so the controller will not launch an agent "
                "whose work it could not publish; nothing was launched. Fix the cause, then "
                "'resume'",
            )
        if problem:
            return self._block(Phase.ANALYZE_EXECUTE, plan, problem)
        if honored is not None and (ref not in honored.refs or honored.refs[ref] != remote_head):
            return self._block(
                Phase.ANALYZE_EXECUTE,
                plan,
                self._unjournaled_branch_text(branch, honored.refs.get(ref), remote_head),
            )
        adopt_url = parse_pr_url(adopt.url).canonical if adopt is not None else ""
        if honored is not None:
            if ref not in honored.prs:
                raise StateError(
                    "the ANALYZE_EXECUTE entry observation records no read of the PRs on "
                    f"{branch!r}"
                )
            recorded = honored.prs[ref] or ""
            if not _same_pr(adopt_url, recorded):
                return self._block(
                    Phase.ANALYZE_EXECUTE,
                    plan,
                    self._unobserved_branch_pr_text(branch, recorded, adopt_url),
                )
        wanted = [base_sha] + ([remote_head] if remote_head and remote_head != base_sha else [])
        try:
            self._git_transport().fetch(wanted)
        except GitTransportError as exc:
            raise VerificationError(
                f"the controller could not fetch {', '.join(wanted)} into the shared object "
                f"store before launching the implementation agent: {exc}. Nothing was "
                "launched; 'resume' fetches again"
            ) from exc
        if honored is None:
            # D4.4: persisted by the pre-launch save, so that a branch head, a
            # marker PR, an unmarked PR on the branch or the default branch
            # found by a later entry is explained by this read or not at all.
            state.entry_observation = EntryObservation(
                Phase.ANALYZE_EXECUTE,
                state.current_issue_url,
                state.current_pr_url,
                {ref: remote_head, f"refs/heads/{default_branch}": base_sha},
                base_sha,
                {render_implementation_marker(state.current_issue_url): None},
                {ref: adopt_url or None},
            ).to_dict()
        self._analyze_entry = _AnalyzeEntry(
            issue_url=state.current_issue_url,
            default_branch=default_branch,
            base_sha=base_sha,
            remote_head=remote_head,
            adopt_url=adopt_url,
        )
        return None

    def _branch_head(self, branch: str) -> str | None:
        """The head of ``branch`` on GitHub, or ``None`` when it does not exist."""
        state = self._require_state()
        try:
            return self.github.get_branch_head_sha(state.repository, branch)
        except GitHubNotFoundError:
            return None

    def _analyze_branch_pr(self, branch: str, default_branch: str) -> tuple[PRInfo | None, str]:
        """The open PR on the controller's branch that ANALYZE_EXECUTE adopts (D9.4).

        ``(None, "")`` when no PR of this repository is headed at ``branch``;
        ``(pr, "")`` for exactly one, open, onto the default branch and
        carrying no implementation marker (the issue's own marker is
        recovered before this is asked); otherwise ``(None, reason)``. A
        closed or merged PR on the branch is never a reason for a second
        one, and two PRs, another base or another issue's marker are never
        guessed around. A fork's PR is never listed (the listing holds
        same-repository heads only), so it is never adopted.
        """
        state = self._require_state()
        prs = self.github.list_prs_for_head(state.repository, branch)
        if not prs:
            return None, ""
        closed = sorted(
            f"{parse_pr_url(p.url).canonical} ({p.state})" for p in prs if not p.is_open
        )
        if closed:
            return None, (
                f"branch {branch!r} already has a closed or merged PR: {', '.join(closed)}. "
                "The controller opens this issue's PR on that branch only, and never a second "
                "one beside a closed or merged PR (ADR 0004 D9.4); nothing was pushed or "
                "created. Reopen that PR to have it adopted, or finish the issue by hand"
            )
        if len(prs) > 1:
            urls = ", ".join(sorted(parse_pr_url(p.url).canonical for p in prs))
            return None, (
                f"{len(prs)} open PRs are headed at {branch!r} ({urls}); the controller never "
                "chooses between them. Close the extra ones, then 'unblock'"
            )
        pr = prs[0]
        url = parse_pr_url(pr.url).canonical
        if pr.base_ref != default_branch:
            return None, (
                f"open PR {url} on {branch!r} targets {pr.base_ref!r}, not the default branch "
                f"{default_branch!r}; the controller adopts only a PR onto the default branch. "
                "Retarget or close it, then 'unblock'"
            )
        found = scan(IMPLEMENTATION, pr.body)
        if found.claims or found.defects:
            return None, (
                f"open PR {url} on {branch!r} carries an implementation marker that is not "
                f"issue #{parse_issue_url(state.current_issue_url).number}'s, or a malformed "
                "one; an adopted PR implements exactly one issue. Repair its body or close it, "
                "then 'unblock'"
            )
        return pr, ""

    def _unjournaled_pr_text(self, url: str) -> str:
        state = self._require_state()
        return (
            f"open PR {url} carries the implementation marker for issue "
            f"{state.current_issue_url}, but the controller did not open it: it appeared "
            "after this phase's entry recorded none, and the controller opens or adopts the "
            "issue's PR itself, from a plan it journals before anything is sent (ADR 0004 "
            "D9.4). Nothing was pushed or created. If that PR is the issue's implementation, "
            "'unblock' binds it and enters REVIEW; otherwise close it, then 'unblock'"
        )

    def _unjournaled_branch_text(self, branch: str, recorded: str | None, found: str | None) -> str:
        return (
            f"branch {branch!r} is at {found or 'nothing (it does not exist)'}, but this "
            f"phase's entry recorded {recorded or 'no such branch'}: something other than the "
            "controller pushed to, created or deleted the branch the controller publishes "
            "this issue to, and it never pushes over a head it did not record (ADR 0004 "
            "D9.4). Nothing was pushed. Inspect the branch, then 'unblock': the entry reads "
            "it again and the agent continues from its head"
        )

    def _default_branch_changed_text(self, planned: str, found: str) -> str:
        return (
            f"the default branch of {self._require_state().repository} is {found!r}, but this "
            f"phase's entry read {planned!r} and the candidate was checked against its head: "
            "the default branch was renamed or switched under the run, and the controller "
            "opens the implementation PR only onto the default branch it read (ADR 0004 K2)"
        )

    def _unobserved_branch_pr_text(self, branch: str, recorded: str, found: str) -> str:
        return (
            f"the open PR on {branch!r} is {found or 'none'}, but this phase's entry recorded "
            f"{recorded or 'none'}: a PR was opened, closed or replaced on the branch the "
            "controller publishes this issue to after the entry read it, and the controller "
            "adopts only the PR its entry observed (ADR 0004 D9.4). Nothing was pushed or "
            "written. Inspect that PR, then 'unblock': the entry reads the branch again and "
            "adopts an open unmarked PR on it, so close the PR first if it is not this issue's"
        )

    def _analyze_prompt_variables(self) -> dict[str, str | int | None]:
        """The ANALYZE_EXECUTE prompt's branch, base and start commit (#161).

        Rendered from the entry's reads; a plan rendered without them (a dry
        run) shows visible placeholders rather than a plausible guess.
        """
        s = self._require_state()
        branch = self.branch_name_for(s.current_issue_url) if s.current_issue_url else "(none)"
        variables: dict[str, str | int | None] = {
            "BRANCH": escape_inline(branch),
            "MAX_PR_TITLE_CHARS": MAX_PR_TITLE_CHARS,
            "MAX_PR_BODY_CHARS": MAX_PR_BODY_CHARS,
        }
        entry = self._analyze_entry
        if entry is None or not same_issue_url(entry.issue_url, s.current_issue_url):
            pending = "(read from GitHub at execution)"
            variables.update(
                {
                    "DEFAULT_BRANCH": pending,
                    "BASE_SHA": pending,
                    "START_SHA": pending,
                    "START_DESCRIPTION": pending,
                }
            )
            return variables
        if entry.remote_head is None:
            start = "the default branch head"
        elif entry.adopt_url:
            start = (
                f"the head of `{escape_inline(branch)}` on GitHub, the branch of open PR "
                f"{entry.adopt_url}, which the controller will adopt"
            )
        else:
            start = (
                f"the head of `{escape_inline(branch)}` on GitHub: earlier work for this "
                "issue, with no PR yet"
            )
        variables.update(
            {
                "DEFAULT_BRANCH": escape_inline(entry.default_branch),
                "BASE_SHA": entry.base_sha,
                "START_SHA": entry.remote_head or entry.base_sha,
                "START_DESCRIPTION": start,
            }
        )
        return variables

    def _analyze_pr_body(self, body: str) -> str:
        """K2's body: the agent's text, then the controller's ``Closes #n`` and marker."""
        return compose_append(body.rstrip(), self._analyze_closing_block())

    def _analyze_closing_block(self) -> str:
        """``Closes #n`` and the issue's implementation marker, as the controller writes them."""
        return implementation_closing_block(self._require_state().current_issue_url)

    def _worktree_git(self, args: list[str], cwd: str, what: str) -> tuple[int, str]:
        """One local git read in the agent's worktree: ``(exit code, stdout)``.

        A timeout, a truncated capture or an exit other than 0 or 1 is
        inconclusive (:class:`VerificationError`): the controller cannot say
        what the worktree holds, and never guesses.
        """
        res = (self._runner or execute)(
            local_git_request(["-C", cwd, *args], timeout_seconds=GIT_TIMEOUT_SECONDS)
        )
        if res.timed_out or res.truncated or res.exit_code not in (0, 1):
            detail = (res.stderr or res.stdout).strip().splitlines()
            raise VerificationError(
                f"cannot read {what} in the agent worktree {cwd} (`git {' '.join(args)}` "
                f"{'timed out' if res.timed_out else f'exited {res.exit_code}'}"
                f"{f': {redact(detail[-1])[:200]}' if detail else ''}); nothing was pushed. "
                "'resume' reads it again"
            )
        return res.exit_code, res.stdout.strip()

    def _check_analyze_result(self, payload: dict, cwd: str) -> AnalyzeExecuteResult:
        """The controller's half of the ANALYZE_EXECUTE schema: the candidate (#161).

        Checked before the result is accepted, so a refusal is corrected
        (the agent is asked again) before anything is pushed or created.
        The candidate is the worktree's ``HEAD`` as the controller reads it:
        detached, equal to the reported ``head_sha``, different from the
        recorded base, descending from it and from the branch head the
        entry recorded (so the push is a fast-forward), with every commit of
        the published range passing the commit-message policy (ADR 0004
        D7.5, D8.5). The composed PR body (the agent's body, ``Closes #n``
        and the marker) passes the credential rule as a whole. A read that
        cannot be completed is inconclusive (:class:`VerificationError`),
        never a correction.
        """
        state = self._require_state()
        entry = self._analyze_entry
        if entry is None or not same_issue_url(entry.issue_url, state.current_issue_url):
            raise StateError("an ANALYZE_EXECUTE result was checked without the entry's reads")
        res = AnalyzeExecuteResult.from_payload(payload)
        issue = parse_issue_url(state.current_issue_url)
        if not same_issue_url(res.issue_url, state.current_issue_url):
            raise ControlResultValidationError(
                f"ANALYZE_EXECUTE: field 'issue_url' names {res.issue_url}, but this run "
                f"implements {issue.canonical}; report the run's issue"
            )
        problem = published_payload_problem("PR body", self._analyze_pr_body(res.pr_body))
        if problem:
            raise ControlResultValidationError(f"ANALYZE_EXECUTE: {problem}")
        attached, _ = self._worktree_git(
            ["symbolic-ref", "-q", "HEAD"], cwd, "whether HEAD is detached"
        )
        if attached == 0:
            raise ControlResultValidationError(
                "ANALYZE_EXECUTE: the worktree's HEAD is attached to a local branch; the "
                "controller publishes a detached HEAD only. Run `git checkout --detach`, keep "
                "your commits, and re-emit the CONTROL_RESULT"
            )
        found, head = self._worktree_git(
            ["rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd, "the HEAD commit"
        )
        if found != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ControlResultValidationError(
                "ANALYZE_EXECUTE: the worktree has no HEAD commit the controller can read; "
                "commit your work and re-emit the CONTROL_RESULT"
            )
        if head != res.head_sha:
            raise ControlResultValidationError(
                f"ANALYZE_EXECUTE: field 'head_sha' is {res.head_sha}, but the worktree's HEAD "
                f"is {head}; report `git rev-parse HEAD`"
            )
        if head == entry.base_sha:
            raise ControlResultValidationError(
                f"ANALYZE_EXECUTE: HEAD {head} is the default branch head itself; commit the "
                "implementation, then re-emit the CONTROL_RESULT"
            )
        transport = self._git_transport()
        try:
            proof = transport.prove_range(entry.base_sha, head)
            follows_branch = (
                entry.remote_head is None
                or entry.remote_head == entry.base_sha
                or transport.descends_from(head, entry.remote_head)
            )
        except GitTransportError as exc:
            raise VerificationError(
                f"the ancestry of the candidate {head} could not be proven: {exc}. Nothing was "
                "pushed; 'resume' proves it again"
            ) from exc
        if not proof.reached_base:
            raise ControlResultValidationError(
                f"ANALYZE_EXECUTE: HEAD {head} does not descend from the default branch head "
                f"{entry.base_sha}; run `git merge --no-edit {entry.base_sha}` (or rebuild your "
                "commits on it) and re-emit the CONTROL_RESULT"
            )
        if not follows_branch:
            raise ControlResultValidationError(
                f"ANALYZE_EXECUTE: HEAD {head} does not descend from {entry.remote_head}, the "
                "head of the branch the controller pushes to, so the push would not be a "
                "fast-forward; continue from that commit and re-emit the CONTROL_RESULT"
            )
        message_problem = published_range_problem(
            proof,
            lambda message: commit_message_problem(
                message, repository=state.repository, issue_number=issue.number
            ),
        )
        if message_problem:
            raise ControlResultValidationError(f"ANALYZE_EXECUTE: {message_problem}")
        self._analyze_candidate = head
        return res

    def _check_fix_result(self, payload: dict, cwd: str) -> FixResult:
        """The controller's half of the REMOTE FIX schema: resolutions and candidate (#163).

        Checked before the result is accepted, so a refusal is corrected (the
        fixer is asked again) before anything is created, appended or
        pushed. The resolutions cover exactly the open findings. A finding
        whose follow-up issue the entry found is resolved as
        ``follow_up_created`` with exactly that issue: the controller reuses
        it and never records a second decision. Any other deferral either
        names an issue the entry handed over (one finding's own follow-up,
        or an earlier round's), never the current issue, or asks for a new
        issue whose composed body passes the credential rule as a whole.
        The candidate is the worktree's ``HEAD`` as the controller reads it:
        detached, equal to ``head_sha``, and either the reviewed HEAD itself
        (nothing committed, so no finding is ``fixed``) or a descendant of
        it whose every published commit passes the commit-message policy,
        and which holds each ``fixed`` finding's named commit (ADR 0004 D7.5,
        D8.5). A read that cannot be completed is inconclusive
        (:class:`VerificationError`), never a correction.
        """
        state = self._require_state()
        res = FixResult.from_payload(payload)
        reviewed = state.reviewed_head_sha.lower()
        issue = parse_issue_url(state.current_issue_url)
        pr_url = parse_pr_url(state.current_pr_url).canonical
        if res.previous_head_sha != reviewed:
            raise ControlResultValidationError(
                f"FIX: field 'previous_head_sha' is {res.previous_head_sha}, but the open "
                f"findings are bound to the reviewed HEAD {reviewed}; report that HEAD"
            )
        open_ids = [str(f["id"]) for f in state.open_findings]
        reported = [r.finding_id for r in res.resolutions]
        missing = [fid for fid in open_ids if fid not in reported]
        unknown = [fid for fid in reported if fid not in open_ids]
        if missing or unknown:
            raise ControlResultValidationError(
                f"FIX: 'resolutions' must resolve exactly the open findings {open_ids}, one "
                f"resolution each; missing {missing}, unknown {unknown}"
            )
        handed = {url for url in self._existing_follow_ups.values()} | {
            parse_issue_url(url).canonical for _, url in self._existing_pr_follow_ups
        }
        new_chars = 0
        for r in res.resolutions:
            fid = r.finding_id
            own = self._existing_follow_ups.get(fid)
            if own is not None:
                if (
                    r.resolution != "follow_up_created"
                    or r.new_follow_up
                    or not same_issue_url(r.follow_up_issue_url, own)
                ):
                    raise ControlResultValidationError(
                        f"FIX: open issue {own} already is the follow-up of {fid}; resolve {fid} "
                        "as follow_up_created with that issue's URL as 'follow_up_issue_url', "
                        "or report status 'blocked' if that issue is wrong"
                    )
                continue
            if r.resolution != "follow_up_created":
                continue
            if r.new_follow_up:
                body = follow_up_issue_body(r.follow_up_body, pr_url, fid)
                problem = published_payload_problem(
                    f"follow-up issue title of {fid}", r.follow_up_title
                ) or published_payload_problem(f"follow-up issue body of {fid}", body)
                if problem:
                    raise ControlResultValidationError(f"FIX: {problem}")
                new_chars += len(r.follow_up_title) + len(body)
                continue
            if same_issue_url(r.follow_up_issue_url, state.current_issue_url):
                raise ControlResultValidationError(
                    f"FIX: the follow-up of {fid} names the current issue {issue.canonical}, "
                    "which is never a follow-up; resolve the finding in this PR, or defer it "
                    "to another issue"
                )
            if parse_issue_url(r.follow_up_issue_url).canonical not in handed:
                listed = ", ".join(sorted(handed)) or "none"
                raise ControlResultValidationError(
                    f"FIX: {r.follow_up_issue_url} (the follow-up of {fid}) is not a follow-up "
                    f"issue the controller handed over for this PR (listed: {listed}); name one "
                    "of those, or ask for a new issue with 'follow_up_issue'"
                )
        if new_chars + FixContext.STORED_BOUND > MAX_EFFECT_STATE_CHARS:
            raise ControlResultValidationError(
                f"FIX: the new follow-up issues hold {new_chars} characters together, more "
                "than the controller journals for one FIX round; shorten their bodies and "
                "re-emit the CONTROL_RESULT"
            )
        attached, _ = self._worktree_git(
            ["symbolic-ref", "-q", "HEAD"], cwd, "whether HEAD is detached"
        )
        if attached == 0:
            raise ControlResultValidationError(
                "FIX: the worktree's HEAD is attached to a local branch; the controller "
                "publishes a detached HEAD only. Run `git checkout --detach`, keep your "
                "commits, and re-emit the CONTROL_RESULT"
            )
        found, head = self._worktree_git(
            ["rev-parse", "--verify", "-q", "HEAD^{commit}"], cwd, "the HEAD commit"
        )
        if found != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise ControlResultValidationError(
                "FIX: the worktree has no HEAD commit the controller can read; check out "
                f"{reviewed} (or your commits on it) and re-emit the CONTROL_RESULT"
            )
        if head != res.head_sha:
            raise ControlResultValidationError(
                f"FIX: field 'head_sha' is {res.head_sha}, but the worktree's HEAD is {head}; "
                "report `git rev-parse HEAD`"
            )
        fixed = [r for r in res.resolutions if r.resolution == "fixed"]
        if head == reviewed:
            if fixed:
                raise ControlResultValidationError(
                    f"FIX: {', '.join(r.finding_id for r in fixed)} resolved as fixed, but HEAD "
                    f"is still the reviewed HEAD {reviewed}: a fixed finding needs a commit. "
                    "Commit the fix, or resolve it another way, and re-emit the CONTROL_RESULT"
                )
            self._fix_candidate = head
            return res
        try:
            proof = self._git_transport().prove_range(reviewed, head)
        except GitTransportError as exc:
            raise VerificationError(
                f"the ancestry of the candidate {head} could not be proven: {exc}. Nothing was "
                "pushed; 'resume' proves it again"
            ) from exc
        if not proof.reached_base:
            raise ControlResultValidationError(
                f"FIX: HEAD {head} does not descend from the reviewed HEAD {reviewed}, so the "
                "push would not be a fast-forward of the PR branch; rebuild your commits on "
                "it (never rewrite the PR's history) and re-emit the CONTROL_RESULT"
            )
        message_problem = published_range_problem(
            proof,
            lambda message: commit_message_problem(
                message, repository=state.repository, issue_number=issue.number
            ),
        )
        if message_problem:
            raise ControlResultValidationError(f"FIX: {message_problem}")
        published = {commit.sha for commit in proof.commits}
        for r in fixed:
            if r.commit_sha and r.commit_sha not in published:
                raise ControlResultValidationError(
                    f"FIX: the commit_sha {r.commit_sha} of {r.finding_id} is not one of the "
                    f"commits after the reviewed HEAD {reviewed[:12]} up to {head[:12]}; name "
                    "the commit that fixed it, or omit commit_sha"
                )
        self._fix_candidate = head
        return res

    # -- operator unblock (issue #5) -------------------------------------------
    def unblock(self, reason: str, *, dry_run: bool = False) -> UnblockOutcome:
        """The operator's explicit exit from BLOCKED.

        BLOCKED is where the controller stops when it cannot determine the
        safe next state on its own; nothing leaves it automatically (`resume`
        refuses terminal phases and an agent never routes out of one). This
        is the one supported way out, and it is the controller's decision,
        not the operator's: the operator supplies ``reason`` (recorded, never
        interpreted), and the controller re-inspects live GitHub state
        exactly as its entry probes do, then either re-enters a phase through
        :func:`validate_transition` -- BLOCKED has operator edges to the
        phases in :data:`transitions.UNBLOCK_TARGETS` and to nothing else --
        or refuses and leaves the run BLOCKED, state untouched, when it still
        cannot tell a safe phase from a guess (a closed PR, two candidate
        PRs, a review bound to another PR, a merge no review decided on, a
        replan journal, an exhausted bound). A refusal names what the
        operator must change.

        Every applied unblock is recorded in ``state.unblock_history`` and in
        the run log; a refusal only in the run log. The step budget is not
        charged: no agent runs here. ``dry_run`` performs the same GitHub
        reads and reports the decision without writing state or the log.
        """
        reason = reason.strip()
        if not reason:
            raise ConfigurationError("unblock requires a non-empty --reason")
        if len(reason) > MAX_UNBLOCK_REASON_CHARS:
            raise ConfigurationError(
                f"unblock --reason must be at most {MAX_UNBLOCK_REASON_CHARS} characters "
                f"({len(reason)} given)"
            )
        if dry_run:
            return self._unblock_once(reason, dry_run=True)
        with self._execution_lock():
            return self._unblock_once(reason, dry_run=False)

    def _unblock_once(self, reason: str, *, dry_run: bool) -> UnblockOutcome:
        state = self._require_state()
        if state.mode == WorkflowMode.LOCAL:
            raise StateTransitionError(
                "unblock applies to GitHub runs only: a LOCAL run has no GitHub state to "
                "re-inspect, so a BLOCKED local run is finished (start a new local run)"
            )
        if state.phase != Phase.BLOCKED:
            raise StateTransitionError(
                f"run {state.run_id} is in phase {state.phase.value}, not BLOCKED; unblock "
                "applies only to BLOCKED runs"
            )
        decision = self._unblock_decision()
        if dry_run:
            if decision.target is None:
                message = f"[dry-run] would stay BLOCKED: {decision.detail}"
            else:
                message = f"[dry-run] would re-enter {decision.target.value}: {decision.detail}"
            return UnblockOutcome(
                run_id=state.run_id,
                unblocked=False,
                phase=state.phase.value,
                message=message,
                dry_run=True,
            )
        cleared = state.block_reason
        target = decision.target
        if target is not None:
            # A pure check on the decided target, made before anything is
            # written: an edge the topology refuses leaves neither a state
            # write nor a run-log record claiming an applied unblock into a
            # phase the run never entered.
            validate_transition(Phase.BLOCKED, target)
        # Logged before the state write: a refused log write leaves the run
        # BLOCKED exactly as it was, and an applied unblock is never on disk
        # without its record. The operator's text and the block reason reach
        # both `state.json` and the log, so both cross the redaction boundary.
        self._log_unblock(reason, decision)
        if decision.refused:
            return UnblockOutcome(
                run_id=state.run_id,
                unblocked=False,
                phase=state.phase.value,
                message=f"stays BLOCKED: {decision.detail}",
            )
        assert target is not None
        pr = decision.pr
        if pr is not None and pr.head_sha:
            state.current_head_sha = pr.head_sha
            if pr.base_ref:
                state.current_base_ref = pr.base_ref
            # The merge base was not read here; the phase entered re-reads
            # it (REVIEW binds it, FIX and the merge gate compare it).
            state.current_merge_base_sha = ""
            if pr.head_ref:
                state.current_branch = pr.head_ref
        carried = ""
        if decision.carry_findings:
            # Same rule as a HEAD that moved under a review
            # (:meth:`_revision_drift_to_review`): the persisted review
            # evidence describes a revision that is no longer the PR, so its
            # result is stale whatever it was -- a clean verdict included,
            # which must not stay recorded as current while the actual
            # revision is reviewed. A PR never reviewed has no result to
            # mark. Its open findings are not dropped but handed to that
            # round to re-check. A carry already performed by a stale round
            # (`prior_findings` set, `open_findings` empty) is preserved
            # untouched: an unblock is not a review round, so no reviewer has
            # examined those findings yet, and the "replace, never
            # accumulate" rule only applies once one has (R3-F1 of PR #101).
            if state.last_review_result:
                state.last_review_result = RESULT_STALE
            if state.open_findings:
                state.prior_findings = state.open_findings
                state.open_findings = []
                carried = (
                    f"; the {len(state.prior_findings)} open finding(s) of round "
                    f"{state.review_round} are carried to that review to re-check"
                )
            elif state.prior_findings:
                carried = (
                    f"; the {len(state.prior_findings)} finding(s) already carried from "
                    f"the stale round {state.review_round} stay carried to that review to "
                    "re-check"
                )
        state.unblock_history.append(
            {
                "at": utcnow_iso(),
                "reason": redact(reason),
                "block_reason": redact(cleared),
                "phase": target.value,
                "detail": redact(decision.detail),
            }
        )
        effects = state.phase_effects()
        if effects.phase == target:
            # D2.4: the records belong to the phase entry the block
            # interrupted, and the unblock resumes it. A conflict the operator
            # resolved is reconciled again, its attempt count kept; any other
            # target drops them with the save below.
            state.effect_records = [r.reopened().to_dict() for r in effects.records]
        state.block_reason = ""
        state.phase = target
        state.attempt = 0
        self._save()
        return UnblockOutcome(
            run_id=state.run_id,
            unblocked=True,
            phase=target.value,
            message=f"BLOCKED -> {target.value}: {decision.detail}{carried}",
        )

    def _log_unblock(self, reason: str, decision: UnblockDecision) -> None:
        """One controller-only run-log record per unblock attempt (applied or refused)."""
        state = self._require_state()
        now = utcnow_iso()
        record = ExecutionRecord(
            run_id=state.run_id,
            seq=0,
            phase="BLOCKED-unblock",
            attempt=1,
            issue_url=state.current_issue_url,
            pr_url=state.current_pr_url,
            review_round=state.review_round,
            profile="(operator unblock; no agent)",
            prompt_version=state.prompt_version,
            started_at=now,
            finished_at=now,
            metadata={
                "operator_reason": reason,
                "block_reason": state.block_reason,
                "applied": not decision.refused,
                "target_phase": decision.target.value if decision.target else "",
                "detail": decision.detail,
                "live_pr_state": decision.pr.state if decision.pr else "",
                "live_head_sha": decision.pr.head_sha if decision.pr else "",
            },
        )
        if decision.refused:
            record.error = f"unblock refused: {decision.detail}"
        self._logger().log_execution(record)

    def _unblock_decision(self) -> UnblockDecision:
        """Re-run the recovery inspection against live GitHub and choose the phase.

        Refuses (``target=None``) whenever the safe phase is not knowable
        from state plus GitHub, in this order: a replan journal (REVIEW is
        that transaction's only entry), an exhausted step budget (the very
        next step would block again), then per the PR. A transient GitHub
        failure propagates unchanged: nothing is decided on a read that may
        succeed next time.
        """
        state = self._require_state()
        if state.replan_transaction:
            stage = state.replan_transaction.get("stage", "(unknown)")
            return UnblockDecision(
                None,
                f"a REPLAN_REEXECUTE journal is recorded for this run (stage {stage!r}); "
                "REVIEW is that transaction's only entry, so unblock cannot re-enter it. "
                "Inspect the journal ('autoforge status') and the PRs it names; this run "
                "cannot continue automatically",
            )
        budget = step_budget_reason(state.step_count, self.config.workflow.max_total_steps)
        if budget:
            return UnblockDecision(
                None,
                f"{budget}. Raise 'workflow.max_total_steps' in the config, then unblock again",
            )
        if not state.current_pr_url:
            return self._unblock_without_pr()
        return self._unblock_with_pr()

    def _unblock_without_pr(self) -> UnblockDecision:
        """No PR bound: ANALYZE_EXECUTE, once its entry probe is known to succeed."""
        state = self._require_state()
        try:
            issue = self._verify_issue_selectable(state.current_issue_url, switching=False)
        except VerificationError as exc:
            return UnblockDecision(None, f"the issue cannot be worked on: {exc}")
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            # The same classification as the bound-PR path: a conclusive
            # failure (authentication, permissions, malformed data) is a
            # decision -- the entry probe is known to fail -- so it is a
            # logged refusal, not an error that escapes the audit record.
            return UnblockDecision(
                None,
                f"issue {state.current_issue_url} cannot be verified: {exc}. This is not a "
                "transient GitHub failure, so re-checking would not help; fix the cause first",
            )
        effects = state.phase_effects()
        if effects.phase == Phase.ANALYZE_EXECUTE and isinstance(effects.context, AnalyzeContext):
            # The agent's commit was accepted and the push and the PR planned:
            # the re-entry completes them from the journal, whatever GitHub
            # holds now, and reconciles each record before anything is sent.
            return UnblockDecision(
                Phase.ANALYZE_EXECUTE,
                f"issue #{issue.number} is OPEN and its push and PR are planned "
                f"({', '.join(r.describe() for r in effects.records)}); ANALYZE_EXECUTE "
                "completes from its persisted plan without launching the agent",
            )
        pr, problem = self._implementation_pr_candidate()
        if problem:
            return UnblockDecision(None, problem)
        if pr is None:
            detail = (
                f"issue #{issue.number} is OPEN and no open PR implements it; ANALYZE_EXECUTE "
                "will launch the implementation agent"
            )
        else:
            detail = (
                f"issue #{issue.number} is OPEN and open PR {parse_pr_url(pr.url).canonical} "
                f"implements it (HEAD {pr.head_sha[:12]}); ANALYZE_EXECUTE will adopt it "
                "without launching the agent"
            )
        return UnblockDecision(Phase.ANALYZE_EXECUTE, detail)

    def _unblock_with_pr(self) -> UnblockDecision:
        """A PR is bound: decide from its live state and the review bound to it."""
        state = self._require_state()
        url = state.current_pr_url
        try:
            pr = self.github.get_pr(url)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return UnblockDecision(
                None,
                f"PR {url} cannot be read: {exc}. This is not a transient GitHub failure, so "
                "re-checking would not help; fix the cause first",
            )
        ref = parse_pr_url(pr.url or url)
        canonical = ref.canonical
        if ref.repository.lower() != state.repository.lower():
            return UnblockDecision(None, f"PR {canonical} is not in repository {state.repository}")
        if pr.url and not ref.same_target(parse_pr_url(url)):
            return UnblockDecision(
                None, f"GitHub answered {url} with PR {canonical}, which is another PR"
            )
        reviewed = (state.reviewed_head_sha or "").lower()
        review_is_this_pr = bool(state.reviewed_pr_url)
        if review_is_this_pr and not parse_pr_url(state.reviewed_pr_url).same_target(ref):
            # The review binding is a decision about one PR. Read against
            # another PR it is neither a clean verdict to merge on nor
            # findings to carry into that PR's review: review evidence never
            # crosses a PR identity boundary, so this is not knowable-safe
            # and is refused before the live state is even considered (the
            # merge gate refuses the same state, from state alone).
            return UnblockDecision(
                None,
                f"the review bound in state is of PR {state.reviewed_pr_url}, not of the "
                f"current PR {canonical}; the controller will not re-enter a phase on "
                "review evidence of another PR. Inspect the state file and both PRs "
                "manually; this run cannot continue automatically",
                pr=pr,
            )
        at_reviewed_revision = (
            review_is_this_pr
            and bool(reviewed)
            and (pr.head_sha or "").lower() == reviewed
            and bool(state.reviewed_base_ref)
            and pr.base_ref == state.reviewed_base_ref
        )
        clean = state.last_review_result == RESULT_CLEAN
        if pr.state == "MERGED":
            if canonical in state.counted_merged_prs:
                return UnblockDecision(
                    Phase.UPDATE_EPIC,
                    f"PR {canonical} is MERGED and already counted; UPDATE_EPIC completes "
                    "from its persisted result when one was saved, and otherwise reads the "
                    "EPIC's progress comments before launching its agent",
                    pr=pr,
                )
            if not (review_is_this_pr and clean and reviewed):
                return UnblockDecision(
                    None,
                    f"PR {canonical} is MERGED, but no clean review of it is bound in state "
                    f"(last_review_result={state.last_review_result!r}); the controller will "
                    "not count a merge no review decided on. Start a new run for the next "
                    "issue and update the EPIC manually",
                    pr=pr,
                )
            problem = self._merged_revision_problem(pr, reviewed, state.reviewed_base_ref)
            if problem:
                return UnblockDecision(
                    None,
                    f"PR {canonical} is MERGED but {problem}; the controller will not count a "
                    "merge no review decided on. Start a new run for the next issue and "
                    "update the EPIC manually",
                    pr=pr,
                )
            return UnblockDecision(
                Phase.READY_FOR_MERGE,
                f"PR {canonical} is MERGED at the clean-reviewed HEAD {reviewed[:12]} into "
                f"{state.reviewed_base_ref!r} but not yet counted; READY_FOR_MERGE with the "
                "merge gate open ('resume --allow-merge') reconciles and counts it once",
                pr=pr,
            )
        if pr.state != "OPEN":
            return UnblockDecision(
                None,
                f"PR {canonical} is {pr.state}; the controller will not choose between "
                "reopening it and reimplementing the issue. Reopen the PR, or start a new "
                "run",
                pr=pr,
            )
        if not pr.head_sha:
            return UnblockDecision(None, f"open PR {canonical} has no readable head SHA", pr=pr)
        if not pr.base_ref:
            return UnblockDecision(None, f"open PR {canonical} has no readable base branch", pr=pr)
        effects = state.phase_effects()
        if effects.phase == Phase.REVIEW and isinstance(effects.context, ReviewContext):
            # The reviewer's verdict was accepted and its comment planned: the
            # re-entry posts or reconciles the comment from the journal and
            # judges the round then. No reviewer is launched for it again.
            return UnblockDecision(
                Phase.REVIEW,
                f"PR {canonical} is OPEN and the comment of review round "
                f"{effects.context.round} is planned "
                f"({', '.join(r.describe() for r in effects.records)}); REVIEW completes from "
                "its persisted plan without launching the reviewer",
                pr=pr,
            )
        if effects.phase == Phase.FIX and isinstance(effects.context, FixContext):
            # The fixer's result was accepted and its writes planned: the
            # re-entry completes them from the journal, and a PR head at the
            # planned candidate is the controller's own push. No fixer is
            # launched for it again.
            records = ", ".join(r.describe() for r in effects.records) or "nothing to send"
            return UnblockDecision(
                Phase.FIX,
                f"PR {canonical} is OPEN and the FIX plan of review round "
                f"{effects.context.round} is saved ({records}); FIX completes from its "
                "persisted plan without launching the fixer",
                pr=pr,
            )
        cap = next_round_cap_reason(state.review_round, self.config.workflow.max_review_rounds)
        if at_reviewed_revision and clean:
            return UnblockDecision(
                Phase.READY_FOR_MERGE,
                f"PR {canonical} is OPEN at the clean-reviewed HEAD {reviewed[:12]} on "
                f"{state.reviewed_base_ref!r}; READY_FOR_MERGE re-verifies it on GitHub before "
                "any merge",
                pr=pr,
            )
        if (
            at_reviewed_revision
            and state.last_review_result == RESULT_NEEDS_FIX
            and (state.open_findings)
        ):
            if cap:
                return UnblockDecision(
                    None,
                    f"{cap}, so a FIX round now could never be reviewed. Raise "
                    "'workflow.max_review_rounds' in the config, then unblock again",
                    pr=pr,
                )
            return UnblockDecision(
                Phase.FIX,
                f"PR {canonical} is OPEN at the reviewed HEAD {reviewed[:12]} with "
                f"{len(state.open_findings)} open finding(s) from round {state.review_round}; "
                "FIX re-reads the HEAD, base and merge base before launching the fixer",
                pr=pr,
            )
        if cap:
            return UnblockDecision(
                None,
                f"{cap}. Raise 'workflow.max_review_rounds' in the config, then unblock again",
                pr=pr,
            )
        if not review_is_this_pr or not reviewed:
            why = "no completed review of it is bound in state"
        elif not at_reviewed_revision:
            why = (
                f"its revision moved after review round {state.review_round} (HEAD "
                f"{pr.head_sha[:12]} on {pr.base_ref!r}, reviewed {reviewed[:12]} on "
                f"{state.reviewed_base_ref!r})"
            )
        else:
            why = f"its last review is {state.last_review_result or 'unrecorded'!r}"
        return UnblockDecision(
            Phase.REVIEW,
            f"PR {canonical} is OPEN and {why}; REVIEW round {state.review_round + 1} "
            "binds the actual revision",
            pr=pr,
            carry_findings=not at_reviewed_revision,
        )

    def _premerge_plan_notes(self) -> list[str]:
        """Dry-run description of the controller's own pre-merge evidence (#42)."""
        notes: list[str] = []
        safety = self.config.safety
        if safety.verify_check_definition and safety.required_checks:
            notes.append(
                "would verify the definition behind each required check "
                f"({', '.join(safety.required_checks)}): a GitHub Actions run at the reviewed "
                "HEAD whose jobs and steps equal the base branch's own push run of that "
                "workflow at its current tip (safety.verify_check_definition)"
            )
        commands = self.config.merge.verification_commands
        if commands:
            shown = "; ".join(" ".join(redact_argv(list(argv))) for argv in commands)
            notes.append(
                "would then export the reviewed HEAD into a temporary directory (no "
                f"worktree, no branch) and run merge.verification_commands there: {shown} "
                "(any failure -> BLOCKED; dry-run runs nothing)"
            )
        else:
            notes.append(
                "no merge.verification_commands configured: nothing runs locally before the "
                "merge, so a green check is the only evidence about what the tests assert"
            )
        return notes

    def _ready_for_merge_step(self, plan: StepPlan, allow_merge: bool) -> StepOutcome:
        """READY_FOR_MERGE -> MERGE only after the full pre-merge verification.

        The same GitHub checks MERGE performs run here first, so a PR that
        GitHub reports as closed, conflicting, failing checks, draft, or
        drifted never enters MERGE at all (MERGE re-verifies anyway: the
        data may change between the two steps).
        """
        state = self._require_state()
        verified = self._verify_pr_for_merge(Phase.READY_FOR_MERGE, plan, allow_merge)
        if isinstance(verified, StepOutcome):
            return verified
        # The PR was just verified to be at the reviewed revision.
        nxt = self._next_phase(Phase.READY_FOR_MERGE, {"head_changed_after_review": False})
        validate_transition(Phase.READY_FOR_MERGE, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.READY_FOR_MERGE,
            plan=plan,
            message=(
                f"merge gate open and GitHub confirms PR {verified.url} is mergeable at the "
                f"reviewed HEAD {verified.head_sha[:12]} on {verified.base_ref!r}; "
                f"READY_FOR_MERGE -> {nxt.value}"
            ),
        )

    def merge_gate_open(self, allow_merge: bool) -> bool:
        """The merge safety gate: config ``safety.allow_merge`` AND the CLI flag."""
        return bool(self.config.merge_allowed_by_config and allow_merge)

    def _check_merge_gate(self, allow_merge: bool) -> None:
        if not self.merge_gate_open(allow_merge):
            raise VerificationError(MERGE_GATE_MESSAGE)

    def _block(self, previous: Phase, plan: StepPlan | None, reason: str) -> StepOutcome:
        """Enter BLOCKED with a human-readable reason (never guess).

        The reason is redacted here, once, not at the call sites: many of
        them quote a `GitHubError` (`gh` stderr, which can echo an
        ``Authorization`` header), a PR body or a command's output, and
        `state.json` is stored in the clear. Redacting at the sink covers a
        new call site by construction and is the same boundary the
        agent-message writers apply; the CLI redacts what it prints from
        state on top of this. It is then bounded, after the redaction and
        for the same reason at the same sink (#88): a call site can
        concatenate agent text (the LOCAL ``unresolved`` rationales), and
        the reason is persisted on every save and printed by ``status``.
        """
        state = self._require_state()
        reason = bound_block_reason(redact(reason))
        state.phase = Phase.BLOCKED
        state.block_reason = reason
        self._save()
        return self._outcome(previous, plan=plan, message=reason)

    def _loop_block_reason(self, reason: str) -> str:
        """BLOCKED text for a REVIEW/FIX loop bound (cap or stagnation)."""
        state = self._require_state()
        return (
            f"{reason}. The run stays on PR {state.current_pr_url or '(none)'} "
            f"(issue {state.current_issue_url}); nothing was merged. A human must inspect the "
            "open findings and the PR (or raise the 'workflow:' bounds in the config)."
        )

    @staticmethod
    def _budget_block_reason(reason: str) -> str:
        return (
            f"{reason}. This budget is cumulative for the run and is not reset by 'resume'; "
            "nothing was merged. Raise 'workflow.max_total_steps' in the config or start a "
            "new run."
        )

    def _budget_may_stop(self, phase: Phase) -> bool:
        """Whether the exhausted step budget blocks ``phase`` before it executes.

        Every phase but one: a REPLAN_REEXECUTE journal that has begun
        closing the source PR is a persisted decision the controller
        finishes (:func:`budget_may_stop`). Blocking it would leave the
        source closed by the controller with nothing in the run's state
        saying so, and ``resume`` refuses BLOCKED, so raising the budget
        could not repair it. The reducer at those stages invokes no agent and
        writes nothing but the one bounded retry of a close the source's
        issue events prove never ran (#69); the budget ends the run at the
        next step instead.
        """
        if phase is not Phase.REPLAN_REEXECUTE:
            return True
        journal = self._require_state().replan_transaction
        return budget_may_stop(ReplanTransaction.from_dict(journal))

    def _replan_finishes_under_budget_note(self, budget: str) -> str:
        """Dry-run note for a replan step that runs although the budget is reached.

        Says only *that* the step runs and how it is charged; *what* it does
        is the stage's own note (:meth:`_replan_plan`), so the two cannot
        describe different actions. The one write finishing may still perform
        is the bounded close retry at ``SUPERSEDE_INTENT`` (#69), and the note
        says so only where the stage note promises it, so it never claims
        "without closing anything" above a note that may close.
        """
        txn = ReplanTransaction.from_dict(self._require_state().replan_transaction)
        if txn.stage in CLOSE_BEGUN_STAGES:
            if (
                txn.stage is ReplanStage.SUPERSEDE_INTENT
                and txn.close_attempts < MAX_CLOSE_ATTEMPTS
            ):
                writes = (
                    "without invoking an agent; the one write it may still perform is the "
                    "bounded retry of the close, and only if the source's closed issue events "
                    "prove the first close never ran"
                )
            else:
                writes = "without invoking an agent and without closing anything"
            return (
                f"step budget reached ({budget}), but the replan transaction has already begun "
                f"closing the source PR {txn.source_pr_url} (stage {txn.stage.value}); would "
                f"finish it as described in the stage note below, {writes}; the step counts "
                "and the budget ends the run at the next step"
            )
        return (
            f"step budget reached ({budget}), but the replan transaction is "
            f"{txn.stage.value}; would replay its refusal, which names the source PR "
            f"{txn.source_pr_url or txn.decision_pr_url or '(unknown)'} and its fate, instead "
            "of blocking with the plain budget text"
        )

    @staticmethod
    def _replan_plan(txn: ReplanTransaction) -> tuple[list[str], str]:
        """What a REPLAN_REEXECUTE step would do, from the journal's stage alone.

        Returns the plan notes and the expected next phase. The reducer
        (:meth:`_drive_replan`) dispatches on the persisted stage, so the plan
        follows the same dispatch rather than describing the whole lifecycle
        at every stage: the agent is launched only while no PR bound to this
        transaction exists (:func:`may_invoke_agent`; the plan carries a
        command only there), the close is performed from ``VERIFIED`` and
        retried at most once from ``SUPERSEDE_INTENT`` when the source's issue
        events prove the first attempt never landed, and every later stage
        reads GitHub and then activates, undoes or refuses -- which of those is
        a GitHub fact the plan cannot read, so it names all of them. Pure:
        reads the journal and nothing else.
        """
        source = txn.source_pr_url or txn.decision_pr_url or "(unknown)"
        replacement = txn.replacement_pr_url or "(none)"
        to_review = ControllerEngine._expected_next(Phase.REPLAN_REEXECUTE)
        if txn.journal_defects:
            return [
                "the persisted replan transaction could not be read whole; would refuse "
                f"without invoking an agent or touching GitHub, saying whether the source PR "
                f"{source} was already closed cannot be determined from local state",
            ], "BLOCKED (unreadable journal; no agent, no GitHub write)"
        if txn.stage is ReplanStage.REJECTED:
            return [
                "would replay the recorded refusal and enter BLOCKED, without invoking an "
                f"agent or touching GitHub; the refusal states what happened to the source PR "
                f"{source}: {txn.rejection_reason or 'refused by verification'}",
            ], "BLOCKED (recorded refusal replayed; no agent, no GitHub write)"
        if txn.stage is ReplanStage.COMPENSATING:
            return [
                f"would undo the close of the source PR {source} (reopen it via gh if it is "
                "still closed, then confirm the reopen), without invoking an agent and without "
                f"closing anything; the replacement {replacement} is not activated",
                "would then enter BLOCKED naming both PRs and the reason the close was undone",
            ], "BLOCKED (close undone; no agent, no second close)"
        if txn.stage is ReplanStage.SUPERSEDED:
            return [
                f"would re-read the replacement {replacement} and the source PR {source} via gh "
                "and activate the replacement only if both checkpoints still hold, without "
                "invoking an agent and without closing anything",
                "if either checkpoint moved, would refuse and enter BLOCKED naming both PRs "
                "(the close stays: it was confirmed correct when it was made)",
            ], f"{to_review} | BLOCKED if a checkpoint moved"
        if txn.stage is ReplanStage.SUPERSEDE_INTENT:
            retry = (
                "source OPEN without the receipt: would re-read its closed issue events via "
                f"gh and compare them with the {txn.source_closed_event_count} recorded at the "
                "close intent; "
            )
            if txn.close_attempts < MAX_CLOSE_ATTEMPTS:
                retry += (
                    "unchanged -> no close has happened since the intent, would retry the "
                    f"close once (attempt {txn.close_attempts + 1} of {MAX_CLOSE_ATTEMPTS}) "
                    "after both checkpoints hold on a fresh gh read and the attempt has been "
                    "persisted; risen -> a close landed and was reopened, would refuse and "
                    "enter BLOCKED naming both PRs"
                )
            else:
                retry += (
                    f"would refuse and enter BLOCKED either way, since {txn.close_attempts} "
                    "attempts are already recorded and the close is retried at most once"
                )
            return [
                f"would re-read the source PR {source} via gh; invokes no agent",
                "source CLOSED and carrying this transaction's close receipt: would confirm both "
                f"checkpoints, then activate the replacement {replacement} or undo the close "
                "if a checkpoint moved",
                "source OPEN and carrying the receipt, or closed without it: would refuse and "
                "enter BLOCKED naming both PRs, since a prior close landed and was reopened, "
                "or the close is not this transaction's",
                retry,
            ], f"{to_review} | BLOCKED (refused, or the close undone)"
        # Before the write. PENDING and PREPARED may still launch the agent;
        # VERIFIED never does.
        agent: list[str]
        if txn.stage is ReplanStage.PENDING:
            agent = [
                "would checkpoint the source PR, its HEAD, the verified default branch, the "
                "review evidence and the PRs that already exist, then invoke the replan agent "
                "(command above) to create the replacement PR",
            ]
        elif txn.stage is ReplanStage.PREPARED:
            agent = [
                "would look for a PR carrying this transaction's marker: none -> invoke the "
                "replan agent (command above) to create the replacement PR from the verified "
                "default branch; one -> verify it without an agent",
            ]
        else:
            agent = [
                f"the replacement {replacement} is already verified; would not invoke an agent",
            ]
        return [
            *agent,
            f"would close the old PR {source} without merging it only after the replacement "
            "carries this transaction's marker, both checkpoints still hold on a fresh gh "
            "read, and the close intent has been persisted",
            "would refuse and enter BLOCKED, closing nothing, if the replacement cannot be "
            "verified",
        ], f"{to_review} | BLOCKED if the replacement cannot be verified"

    @staticmethod
    def _local_budget_block_reason(reason: str) -> str:
        return (
            f"{reason}. This budget is cumulative for the run, is not reset by 'resume', and "
            "is part of the run's contract: raising 'workflow.max_total_steps' in the config "
            "does not extend this run. Start a new run under the larger budget."
        )

    def _inconclusive(
        self,
        phase: Phase,
        plan: StepPlan | None,
        reason: str,
        cause: BaseException | None = None,
    ) -> StepOutcome:
        """GitHub data is inconclusive: fail closed, bounded, never guessed.

        Persists ``attempt + 1`` and raises VerificationError so the run stays
        in ``phase`` for ``resume``. Once ``merge.max_verification_attempts``
        is reached the run enters BLOCKED instead (returned outcome).
        """
        state = self._require_state()
        state.attempt += 1
        limit = self.config.merge.max_verification_attempts
        if state.attempt >= limit:
            return self._block(
                phase,
                plan,
                f"{reason}. GitHub data stayed inconclusive for {state.attempt} verification "
                f"attempt(s) (merge.max_verification_attempts={limit}); giving up. Nothing "
                "was merged or counted; inspect the PR on GitHub manually.",
            )
        self._save()
        raise VerificationError(
            f"{reason}. Not merging; the run stays in {phase.value} (verification attempt "
            f"{state.attempt}/{limit}) — 'resume --allow-merge' to re-check."
        ) from cause

    @staticmethod
    @contextmanager
    def _reading(what: str) -> Iterator[None]:
        """Name the read a GitHubError came from without losing its class.

        The engine classifies pre-merge read failures by type (transient ->
        bounded re-check, anything else -> BLOCKED), so the wrapper must
        re-raise the *same* class; only the message gains the identity of
        the read, which is what a human then reads in ``block_reason``.
        """
        try:
            yield
        except GitHubUnavailableError as exc:
            raise GitHubUnavailableError(f"{what} could not be read: {exc}") from exc
        except GitHubError as exc:
            raise GitHubError(f"{what} could not be read: {exc}") from exc

    def _github_read_failed(
        self, phase: Phase, plan: StepPlan | None, what: str, exc: GitHubError
    ) -> StepOutcome:
        """A GitHub read the pre-merge verification depends on failed: classify, never guess.

        Transient failures (:class:`GitHubUnavailableError`: timeouts, connection
        errors, 5xx, rate limiting) leave the facts *inconclusive* and take the
        same bounded re-check path as unknown mergeability. Anything else
        (authentication, permissions, a PR that no longer resolves, malformed
        data) is conclusive: re-running would not change it, so the run is
        BLOCKED for a human. Nothing is merged or counted on either path.
        """
        if isinstance(exc, GitHubUnavailableError):
            return self._inconclusive(phase, plan, f"{what} (GitHub unavailable: {exc})", cause=exc)
        return self._block(
            phase,
            plan,
            f"{what}: {exc}. This is not a transient GitHub failure (authentication, "
            "permissions, or the PR itself), so re-checking would not help; nothing was "
            "merged or counted. Fix the cause and inspect the PR on GitHub manually.",
        )

    def _verify_pr_for_merge(
        self, phase: Phase, plan: StepPlan, allow_merge: bool
    ) -> PRInfo | StepOutcome:
        """Controller-side pre-merge verification shared by READY_FOR_MERGE and MERGE.

        Reads nothing from any agent: state + GitHub only. Returns the
        ``PRInfo`` only when every fact the controller can read says the
        reviewed HEAD can be merged synchronously right now. Otherwise the
        transition has already been applied and its outcome is returned:

        - merge gate closed / no clean review bound to a PR, HEAD, base and
          merge base -> raises, nothing changes
        - ``current_pr_url`` is not the reviewed PR by identity, or GitHub
          answers the reviewed URL with a different PR -> BLOCKED before
          anything else is read from it: the review is never re-bound to
          another PR, whatever its HEAD
        - PR already MERGED: from MERGE this is crash recovery (counted once
          if it merged at the reviewed HEAD *and* base, else BLOCKED); from
          READY_FOR_MERGE -> MERGE so that phase reconciles
        - PR CLOSED, draft, conflicting, failing check, branch protection,
          auto-merge armed, merge queue -> BLOCKED (conclusive; no retry)
        - PR HEAD != reviewed HEAD, PR base != reviewed base, or the merge
          base of the two != the reviewed merge base (the base branch was
          rewritten under its name, #96) -> REVIEW (clean review is stale)
        - the review comment the state names, re-read from GitHub, is not
          the clean review of this round at this revision (gone, on another
          PR, marker for another round / HEAD / base / merge base, unreadable marker,
          or ``needs_fix_round`` true) -> BLOCKED (conclusive; the state's
          claim of a clean review is not backed by GitHub, #94)
        - inconclusive (mergeability UNKNOWN, checks running, GitHub read
          failed transiently) -> raises and keeps the phase, bounded by
          ``merge.max_verification_attempts``
        - GitHub read failed conclusively (auth, permissions, unresolvable
          PR or comment) -> BLOCKED
        """
        state = self._require_state()
        self._check_merge_gate(allow_merge)
        if not state.current_pr_url:
            raise StateError(f"phase {phase.value} requires current_pr_url in state")
        url = state.current_pr_url
        reviewed = (state.reviewed_head_sha or "").lower()
        reviewed_base = state.reviewed_base_ref
        reviewed_merge_base = state.reviewed_merge_base_sha
        if (
            state.last_review_result != "clean"
            or not reviewed
            or not state.reviewed_pr_url
            or not reviewed_base
            or not reviewed_merge_base
            or not state.last_review_comment_url
        ):
            raise VerificationError(
                f"{phase.value} requires a clean review bound to a PR, a HEAD, a base "
                f"branch and a merge base, and its review comment, in state "
                f"(last_review_result={state.last_review_result!r}, "
                f"reviewed_pr_url={state.reviewed_pr_url!r}, "
                f"reviewed_head_sha={state.reviewed_head_sha!r}, "
                f"reviewed_base_ref={reviewed_base!r}, "
                f"reviewed_merge_base_sha={reviewed_merge_base!r}, "
                f"last_review_comment_url={state.last_review_comment_url!r}); "
                "refusing to merge"
            )
        # The clean review is a decision about one PR. A `current_pr_url`
        # that names another PR -- even one at the reviewed HEAD, i.e. the
        # same branch proposed against another base -- is not what the
        # review decided on and is refused before GitHub is even asked
        # about it; the binding is never moved to the current URL.
        reviewed_ref = parse_pr_url(state.reviewed_pr_url)
        if not parse_pr_url(url).same_target(reviewed_ref):
            return self._block(
                phase,
                plan,
                f"current_pr_url {url} is not the PR the clean review was posted on "
                f"({state.reviewed_pr_url}); the review is bound to that PR at HEAD "
                f"{reviewed[:12]} on base {reviewed_base!r} and is not re-bound. Nothing "
                "was merged or counted; inspect the state file and the PR manually.",
            )
        try:
            pr = self.github.get_pr(url)
        except GitHubError as exc:
            return self._github_read_failed(phase, plan, f"PR {url} could not be read", exc)
        if parse_pr_url(pr.url or url).repository.lower() != state.repository.lower():
            raise VerificationError(f"PR {url} is not in {state.repository}")
        if pr.url and not parse_pr_url(pr.url).same_target(reviewed_ref):
            return self._block(
                phase,
                plan,
                f"GitHub answered {url} with PR {pr.url}, which is not the reviewed PR "
                f"{state.reviewed_pr_url}; refusing to merge a PR no review decided on. "
                "Nothing was merged or counted.",
            )
        if pr.state == "MERGED":
            if phase == Phase.READY_FOR_MERGE:
                # Whether it merged at the reviewed revision is MERGE's own
                # verification; this step only hands the reconciliation over.
                nxt = self._next_phase(Phase.READY_FOR_MERGE, {"head_changed_after_review": False})
                validate_transition(Phase.READY_FOR_MERGE, nxt)
                state.phase = nxt
                state.attempt = 0
                self._save()
                return self._outcome(
                    phase,
                    plan=plan,
                    message=(
                        f"PR {url} is already MERGED on GitHub; READY_FOR_MERGE -> {nxt.value} "
                        "to reconcile (nothing counted yet)"
                    ),
                )
            # Crash recovery: the merge happened but state was not persisted.
            unreviewed = self._merged_revision_problem(pr, reviewed, reviewed_base)
            if unreviewed:
                return self._block(
                    phase,
                    plan,
                    f"PR {url} is already MERGED {unreviewed}; the controller never "
                    "reviewed the merged change. Inspect manually.",
                )
            return self._complete_merge(pr, plan, recovered=True)
        if not pr.is_open:
            return self._block(
                phase, plan, f"PR {url} is {pr.state}; only an OPEN PR can be merged"
            )
        if not pr.head_sha:
            raise VerificationError(f"PR {url} has no readable head SHA")
        if not pr.base_ref:
            return self._block(
                phase,
                plan,
                f"PR {url} reports no base branch, so the reviewed change against "
                f"{reviewed_base!r} cannot be confirmed as this PR's diff. Nothing was "
                "merged or counted.",
            )
        if pr.head_sha != reviewed or pr.base_ref != reviewed_base:
            return self._revision_drift_to_review(phase, plan, pr)
        # Same HEAD, same base name: the diff is still the reviewed one only
        # if the base branch is still the history the review was computed
        # against. A base rewritten under its name (force-push, reset) moves
        # the merge base and with it the diff (#96); commits merely landing
        # on the base do not, and a PR that fell BEHIND is the readiness
        # check's business, not this one's.
        try:
            merge_base = self._merge_base_of(pr)
        except GitHubError as exc:
            return self._github_read_failed(
                phase, plan, f"the pre-merge merge-base read for PR {url} failed", exc
            )
        if merge_base != reviewed_merge_base:
            return self._revision_drift_to_review(phase, plan, pr, merge_base_sha=merge_base)
        # The PR is at the reviewed revision; now the review itself. Every
        # other fact the gate relies on was just read from GitHub, and the
        # clean review must be too (#94): the comment the state names is
        # re-read and must still be this round's review of this revision,
        # saying clean, on this PR. A state whose review fields were edited
        # after the round -- by hand or by a controller bug -- is refused
        # here, before the PR's own code is run or anything is written.
        try:
            unbacked = self._clean_review_problem(reviewed_ref)
        except GitHubError as exc:
            return self._github_read_failed(
                phase, plan, f"the pre-merge review-comment read for PR {url} failed", exc
            )
        if unbacked:
            return self._block(
                phase,
                plan,
                f"{unbacked}. The clean review recorded in state is not backed by GitHub, "
                "so re-checking would not help; nothing was merged or counted. Inspect the "
                "state file and the PR's review comments manually.",
            )
        try:
            not_ready = self._merge_readiness_problem(pr)
        except VerificationError as exc:
            return self._inconclusive(phase, plan, str(exc), cause=exc)
        except GitHubError as exc:
            return self._github_read_failed(
                phase, plan, f"a pre-merge GitHub read for PR {url} failed", exc
            )
        if not_ready:
            return self._block(phase, plan, f"{not_ready}. Nothing was merged or counted.")
        # Last, because it executes the PR's own code: only a PR that every
        # GitHub-side fact already accepts gets run on the operator's machine.
        try:
            unverified = self._local_verification_problem(phase, pr)
        except VerificationError as exc:
            return self._inconclusive(phase, plan, str(exc), cause=exc)
        if unverified:
            return self._block(phase, plan, f"{unverified}. Nothing was merged or counted.")
        return pr

    def _clean_review_problem(self, reviewed_ref: GitHubPullRequestRef) -> str:
        """Re-read the clean review's comment from GitHub; "" when it backs the state.

        The state says round ``review_round`` reviewed ``reviewed_head_sha``
        against ``reviewed_base_ref`` on ``reviewed_pr_url`` and found it
        clean, and names the comment that decided so
        (``last_review_comment_url``). :meth:`_apply_review` verified all of
        that when the round ended, but the file is plain JSON on a laptop
        and the merge gate is the one place the review is consumed, so the
        comment is read again here, once per gate pass, and must still say
        what the state says:

        - the URL in state parses as a comment URL on the reviewed PR, and
          GitHub answers it with that comment on that PR (the comments API
          addresses a comment by id alone, so the parent GitHub reports is
          the one that counts);
        - the comment carries exactly one readable ``ai-review-result``
          marker, and its key is this round at this HEAD against this base
          from this merge base (the same key the round's read-back matched,
          through the same scanner: :mod:`autoforge.claims`);
        - the marker says ``needs_fix_round: false``.

        Returns the reason the review is *not* backed (the caller BLOCKS:
        conclusive, a re-read would say the same). Raises the GitHubError of
        the read for the caller to classify (transient -> bounded re-check,
        anything else, including a comment GitHub no longer has -> BLOCKED).
        Uniqueness of the marker across the PR's comments is not re-asked:
        the round's read-back established it, and a second comment appearing
        later cannot make the recorded one say something else.
        """
        state = self._require_state()
        url = state.last_review_comment_url
        try:
            cref = parse_comment_url(url)
        except ConfigurationError as exc:
            return f"last_review_comment_url {url!r} is not a GitHub PR comment URL ({exc})"
        if not cref.on(reviewed_ref):
            return (
                f"the review comment recorded in state ({cref.canonical}) is not on the "
                f"reviewed PR {reviewed_ref.canonical}"
            )
        with self._reading(f"review comment {cref.canonical}"):
            comment = self.github.get_comment(cref.canonical)
        try:
            located = parse_comment_url(comment.url)
        except ConfigurationError as exc:
            raise GitHubError(
                f"GitHub answered {cref.canonical} with a comment whose URL cannot be read ({exc})"
            ) from exc
        if not located.same_target(cref) or not located.on(reviewed_ref):
            return (
                f"GitHub answered {cref.canonical} with comment {located.canonical}, which is "
                f"not that comment on the reviewed PR {reviewed_ref.canonical}"
            )
        reviewed = (state.reviewed_head_sha or "").lower()
        base = state.reviewed_base_ref
        merge_base = state.reviewed_merge_base_sha
        what = (
            f"round {state.review_round} at HEAD {reviewed[:12]} on base {base!r} from merge "
            f"base {merge_base[:12]} of PR {reviewed_ref.canonical}"
        )
        try:
            holder = (
                collect(REVIEW, [comment], "comment")
                .claimants((state.review_round, reviewed, base, merge_base), what)
                .exactly_one()
            )
        except ClaimConflictError as exc:
            return f"review comment {located.canonical} is not the clean review in state: {exc}"
        if holder.claim.needs_fix_round:
            return (
                f"review comment {located.canonical} for {what} says needs_fix_round: true, "
                "but the state records the round as clean"
            )
        return ""

    def _local_verification_problem(self, phase: Phase, pr: PRInfo) -> str:
        """Run ``merge.verification_commands`` on the reviewed commit, exported privately.

        The hosted check ran the PR's own code, so nothing GitHub reports can
        tell a weakened test suite from a passing one. These commands are the
        controller's own evidence: the exact reviewed commit (``pr.head_sha``,
        already proven equal to ``reviewed_head_sha``) is written out with
        ``git read-tree`` + ``git checkout-index`` into a fresh temporary
        directory -- never the operator's checkout, no worktree, no branch,
        no ``.git`` inside -- and every command runs there in order, through
        the normal executor (argv only, never a shell), under
        ``execution.command_timeout_seconds``. The directory is deleted
        afterwards whatever happened.

        Returns a non-empty reason when a command failed or timed out (the
        caller BLOCKS: re-running would not change the code). Raises
        VerificationError when the commit cannot be materialised (not local
        and not fetchable as ``refs/pull/<n>/head``, or the export itself
        failed): that is inconclusive, and the caller bounds the re-checks.
        A pass is persisted against the HEAD and the command list so MERGE
        does not repeat what READY_FOR_MERGE already proved for the same
        commit; a different HEAD or a different command list runs again.
        Returns "" when nothing is configured.
        """
        argvs = [list(argv) for argv in self.config.merge.verification_commands]
        if not argvs:
            return ""
        state = self._require_state()
        if (
            state.premerge_verified_head_sha == pr.head_sha
            and state.premerge_verified_commands == argvs
        ):
            return ""
        runner = self._runner or execute
        repo = self.workdir
        sha = pr.head_sha
        if not commit_is_local(runner, repo, sha):
            try:
                fetch_pr_head(self._git_transport(), pr.number)
            except VerificationError as exc:
                raise VerificationError(
                    f"the reviewed HEAD {sha[:12]} of PR {pr.url} is not in the local "
                    f"repository and fetching refs/pull/{pr.number}/head failed: {exc}"
                ) from exc
            if not commit_is_local(runner, repo, sha):
                raise VerificationError(
                    f"the reviewed HEAD {sha[:12]} of PR {pr.url} is still not in the local "
                    f"repository after fetching refs/pull/{pr.number}/head"
                )
        try:
            with export_commit_tree(runner, repo, sha) as exported:
                for argv in argvs:
                    problem = self._run_premerge_command(phase, pr, argv, str(exported.root))
                    if problem:
                        return problem
        except VerificationError as exc:
            raise VerificationError(
                f"the reviewed HEAD {sha[:12]} of PR {pr.url} could not be exported for "
                f"merge.verification_commands: {exc}"
            ) from exc
        state.premerge_verified_head_sha = sha
        state.premerge_verified_commands = argvs
        self._save()
        return ""

    def _git_transport(self) -> GitTransport:
        """The controller's own git transport for this repository (ADR 0004 D7.1).

        Objects land in the shared store of the checkout's git common dir;
        the remote is an explicit URL and the credential comes from ``gh``,
        so no repository configuration, hook or ``origin`` steers it.
        GitHub's ancestry answers come through the typed compare read. A
        checkout that is not a git repository is inconclusive
        (:class:`VerificationError`), never guessed around.
        """
        identity = self._worktree_identity(Path(self.workdir))
        if identity is None:
            raise VerificationError(
                f"{self.workdir} is not inside a git repository; the controller has no object "
                "store to fetch into"
            )
        state = self._require_state()
        repository = state.repository
        github = self.github
        try:
            remote = self._git_remote or GitRemote.https(repository)
        except GitTransportError as exc:
            raise VerificationError(str(exc)) from exc
        return GitTransport(
            object_directory=identity[1] / "objects",
            remote=remote,
            gh_command=self.config.github.command,
            in_base_history=lambda base, sha: github.commit_in_history(repository, base, sha),
            runner=self._runner,
        )

    def _run_premerge_command(self, phase: Phase, pr: PRInfo, argv: list[str], cwd: str) -> str:
        """One ``merge.verification_commands`` entry in the exported tree; "" on exit 0."""
        state = self._require_state()
        # Repository-defined commands run under the same allow-listed
        # environment as an agent: they are the PR's code, not the operator's.
        req = ExecutionRequest(
            command=list(argv),
            cwd=cwd,
            timeout_seconds=self.config.execution.command_timeout_seconds,
            env_allowlist=self.config.execution.environment_names(),
            contain_orphans=True,
        )
        result = (self._runner or execute)(req)
        record = ExecutionRecord(
            run_id=state.run_id,
            seq=0,
            phase=f"{phase.value}-premerge-verification",
            attempt=state.attempt,
            review_round=state.review_round,
            profile="(controller pre-merge verification command)",
            prompt_version=state.prompt_version,
            command=list(argv),
            cwd=cwd,
            timeout_seconds=req.timeout_seconds,
            started_at=result.started_at,
            finished_at=result.finished_at,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            stdout_truncated=result.stdout_truncated,
            stderr_truncated=result.stderr_truncated,
            descendants_killed=result.descendants_killed,
            group_survived_kill=result.group_survived_kill,
            capture_abandoned=result.capture_abandoned,
            orphans_killed=result.orphans_killed,
            orphan_survived_kill=result.orphan_survived_kill,
            orphans_unchecked=result.orphans_unchecked,
            metadata={
                "verification_command": list(argv),
                "pr_url": pr.url,
                "head_sha": pr.head_sha,
            },
        )
        if result.timed_out or result.exit_code != 0:
            record.error = _with_leftovers(
                f"timed out after {req.timeout_seconds}s"
                if result.timed_out
                else f"exit {result.exit_code}",
                result.leftovers,
            )
        self._logger().log_execution(record, "", result.stdout or "", result.stderr or "")
        # The command line and its output both reach `block_reason` in plain
        # `state.json`, so both are redacted here as well as on the log path.
        shown = " ".join(redact_argv(list(argv)))
        if result.timed_out:
            return _with_leftovers(
                f"pre-merge verification command {shown!r} timed out after "
                f"{req.timeout_seconds}s on the reviewed HEAD {pr.head_sha[:12]} of PR "
                f"{pr.url}; the green check is not corroborated locally",
                result.leftovers,
            )
        if result.exit_code != 0:
            tail = redact((result.stderr or result.stdout or "").strip())[-2000:]
            return (
                _with_leftovers(
                    f"pre-merge verification command {shown!r} failed with exit "
                    f"{result.exit_code} on the reviewed HEAD {pr.head_sha[:12]} of PR {pr.url}; "
                    "the green check is not corroborated locally",
                    result.leftovers,
                )
                + f". Output tail: {tail}"
            )
        return ""

    def _revision_drift_to_review(
        self,
        phase: Phase,
        plan: StepPlan,
        pr: PRInfo,
        detail: str = "",
        message: str = "",
        merge_base_sha: str = "",
    ) -> StepOutcome:
        """The OPEN PR's revision (HEAD, base or merge base) is no longer the reviewed one.

        The last review is stale: persists the PR's current HEAD and base
        (and the merge base, when the caller read it: ``merge_base_sha``;
        otherwise it is cleared, the REVIEW entry re-reads it), marks the
        review stale and routes back to REVIEW (``phase -> REVIEW``), where
        the next round is bound to the actual revision. The
        review's open findings, if any (the FIX entry case), stop being open
        -- no fixer resolves findings of a commit that is no longer the PR --
        and become the findings that review is told to re-check
        (``prior_findings``), so an unrecorded push never makes a finding
        disappear unexamined. Nothing has been merged or counted.
        ``message`` replaces the default merge-path wording.
        """
        state = self._require_state()
        nxt, what, carried = self._stale_revision(phase, pr, merge_base_sha)
        validate_transition(phase, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            phase,
            plan=plan,
            message=(
                message
                or (
                    f"{what} after the clean review{detail}; {phase.value} -> {nxt.value} "
                    "(not merged)"
                )
            )
            + carried,
        )

    def _stale_revision(
        self, phase: Phase, pr: PRInfo, merge_base_sha: str = ""
    ) -> tuple[Phase, str, str]:
        """Record that the reviewed revision is no longer the PR's: ``(next, what, carried)``.

        The state half of :meth:`_revision_drift_to_review`, shared with the
        FIX completion, which routes through its caller's transition
        (:meth:`_fix_drift`). Leaves the phase and the save to the caller.
        """
        state = self._require_state()
        if pr.base_ref and pr.base_ref != state.reviewed_base_ref:
            what = f"PR base changed to {pr.base_ref!r}"
        elif pr.head_sha.lower() != (state.reviewed_head_sha or "").lower():
            what = "PR HEAD moved"
        else:
            what = (
                f"PR merge base moved to {merge_base_sha[:12]} from the reviewed "
                f"{state.reviewed_merge_base_sha[:12]} (base {pr.base_ref!r} was rewritten "
                "under its name)"
            )
        state.current_head_sha = pr.head_sha
        if pr.base_ref:
            state.current_base_ref = pr.base_ref
        state.current_merge_base_sha = merge_base_sha
        state.last_review_result = "stale"
        state.prior_findings = state.open_findings
        state.open_findings = []
        carried = (
            f"; the {len(state.prior_findings)} finding(s) of round {state.review_round} are "
            "carried to that review to re-check"
            if state.prior_findings
            else ""
        )
        # FIX's only edge is REVIEW; READY_FOR_MERGE and MERGE route there on
        # the observation that the reviewed revision is no longer the PR's.
        return self._next_phase(phase, {"head_changed_after_review": True}), what, carried

    @staticmethod
    def _merged_revision_problem(pr: PRInfo, reviewed: str, reviewed_base: str) -> str:
        """Why a MERGED PR is not the reviewed revision, or "" when it is.

        A merge is counted only when GitHub says the PR merged at the
        reviewed HEAD *into* the reviewed base: the same commits merged into
        another branch are a change no review decided on.
        """
        if pr.head_sha != reviewed:
            return f"at HEAD {pr.head_sha} but the last clean review covered {reviewed}"
        if pr.base_ref != reviewed_base:
            return (
                f"into {pr.base_ref!r} but the last clean review covered the change "
                f"against {reviewed_base!r}"
            )
        return ""

    # -- MERGE: controller-owned, no agent ------------------------------------------
    def _merge_step(self, plan: StepPlan, allow_merge: bool) -> StepOutcome:
        """Merge the current PR with ``gh pr merge`` — the controller, never an agent.

        Order of checks (all before any write):
        1. merge gate (config AND CLI flag)
        2. state carries a clean review bound to a PR, a HEAD, a base and a
           merge base
        3. ``current_pr_url`` is that PR by identity, and so is the PR GitHub
           returns for it (else BLOCKED; the review is never re-bound)
        4. PR belongs to this repository; already MERGED -> crash recovery
        5. PR is OPEN, its HEAD equals the reviewed HEAD, its base is the
           reviewed base and their merge base is the reviewed merge base
           (else -> REVIEW)
        6. GitHub says the PR is mergeable *now*: not a draft, ``mergeable``
           is MERGEABLE, ``mergeStateStatus`` is CLEAN/HAS_HOOKS, every check
           in the status rollup succeeded, no auto-merge is armed and the
           base branch does not use a merge queue (``gh pr merge`` would
           otherwise arm auto-merge / enqueue instead of merging).
           Conclusive negatives -> BLOCKED. Inconclusive data (mergeability
           UNKNOWN, checks still running, a PR / merge-queue read that failed
           transiently) raises VerificationError and leaves the run in MERGE
           so ``resume --allow-merge`` re-checks later, at most
           ``merge.max_verification_attempts`` times, then BLOCKED. A read
           that failed conclusively (auth, permissions, unresolvable PR) is
           BLOCKED at once. Never guessed.
        Steps 1-6 are :meth:`_verify_pr_for_merge`, shared with READY_FOR_MERGE.
        Then ``gh pr merge --<method> --match-head-commit <reviewed>``; the PR
        is re-read and the merge is counted only when GitHub says MERGED at
        the reviewed HEAD into the reviewed base.
        If the re-read itself fails the outcome is *uncertain*: the run stays
        in MERGE (not BLOCKED, until the same bound is reached) and ``resume``
        reconciles from real GitHub state (already MERGED -> recovered and
        counted once; still OPEN -> re-verified and re-attempted). If the
        re-read finds the PR still OPEN at a *different* HEAD (pushed between
        the verification and the write; ``--match-head-commit`` refused it)
        or against a different base (retargeted in that window; nothing
        guards the base on the write, so this is only reachable when the
        merge itself did not happen), or at the same HEAD and base but from
        a different merge base (the base rewritten in that window, #96),
        nothing unreviewed was merged and the
        revision-drift rule applies: MERGE -> REVIEW, unless that call left
        an asynchronous merge pending (auto-merge / merge queue) that could
        still land the new revision, in which case BLOCKED. Any other
        conclusive non-merge is BLOCKED; merge failures are never blindly
        retried.
        """
        state = self._require_state()
        verified = self._verify_pr_for_merge(Phase.MERGE, plan, allow_merge)
        if isinstance(verified, StepOutcome):
            return verified
        url = state.current_pr_url
        reviewed = (state.reviewed_head_sha or "").lower()
        reviewed_base = state.reviewed_base_ref
        reviewed_merge_base = state.reviewed_merge_base_sha

        merge_error = ""
        try:
            self.github.merge_pr(
                url,
                method=self.config.merge.method,
                match_head_sha=reviewed,
                delete_branch=self.config.merge.delete_branch,
            )
        except GitHubError as exc:
            merge_error = str(exc)

        # GitHub is the source of truth: `gh` exit status is only a hint.
        try:
            after = self.github.get_pr(url)
        except GitHubError as exc:
            # Uncertain outcome (the merge may or may not have happened).
            # Stay in MERGE so `resume` reconciles from real GitHub state
            # instead of terminalising a possibly-successful merge (bounded).
            hint = f"gh pr merge failed ({merge_error}) and" if merge_error else "after gh pr merge"
            return self._inconclusive(
                Phase.MERGE,
                plan,
                f"{hint} the PR could not be re-read: {exc}. Merge outcome unknown; nothing "
                "was counted (an already-merged PR is recovered and counted once on re-check)",
                cause=exc,
            )
        if after.state != "MERGED":
            if merge_error:
                reason = f"gh pr merge failed: {merge_error}"
            else:
                reason = (
                    f"gh pr merge exited 0 but GitHub reports PR {url} as {after.state} "
                    "(auto-merge armed or merge queue?)"
                )
            note, pending = self._disarm_async_merge(url, after)
            drifted = after.is_open and (
                (after.head_sha and after.head_sha != reviewed)
                or (after.base_ref and after.base_ref != reviewed_base)
            )
            merge_base = ""
            if after.is_open and not drifted and after.head_sha and after.base_ref:
                # Same HEAD and base name: the base may still have been
                # rewritten under its name in the window (#96).
                try:
                    merge_base = self._merge_base_of(after)
                except GitHubError as exc:
                    note += f"; {exc}"
                else:
                    drifted = merge_base != reviewed_merge_base
            if drifted and not pending:
                # Post-verification race: the revision moved between the
                # controller's verification and the write (a push, which
                # `--match-head-commit <reviewed>` refused, a retarget, or a
                # rewritten base). Nothing unreviewed merged; the clean
                # review is stale -> REVIEW (same rule as pre-write drift).
                return self._revision_drift_to_review(
                    Phase.MERGE,
                    plan,
                    after,
                    detail=(
                        f" (HEAD {after.head_sha[:12]} on {after.base_ref!r} from merge base "
                        f"{merge_base[:12] or '(unread)'} != reviewed {reviewed[:12]} on "
                        f"{reviewed_base!r} from {reviewed_merge_base[:12]}; {reason}{note})"
                    ),
                    merge_base_sha=merge_base,
                )
            return self._block(
                Phase.MERGE,
                plan,
                f"{reason}{note}. Nothing was counted; resolve on GitHub manually.",
            )
        unreviewed = self._merged_revision_problem(after, reviewed, reviewed_base)
        if unreviewed:
            return self._block(
                Phase.MERGE,
                plan,
                f"PR {url} is MERGED {unreviewed}; refusing to count an unreviewed merge. "
                "Inspect manually.",
            )
        return self._complete_merge(after, plan, recovered=False)

    def _merge_readiness_problem(self, pr: PRInfo) -> str:
        """Controller-side pre-merge verification against GitHub (fail closed).

        Returns a non-empty reason when the PR must NOT be merged (-> BLOCKED).
        Raises VerificationError when GitHub's data is inconclusive (the
        caller keeps the phase and bounds the re-checks) and lets GitHubError
        from the changed-file / merge-queue reads propagate (the caller classifies it:
        transient -> same bounded path, conclusive -> BLOCKED). Returns "" only when
        every fact the controller can read says a synchronous merge of this
        exact HEAD is acceptable right now.
        """
        url = pr.url
        if pr.is_draft:
            return f"PR {url} is a draft"

        # 1. the PR must not redefine the very checks this gate trusts.
        redefines = self._protected_path_problem(pr)
        if redefines:
            return redefines

        # 2. checks: all of them, not only the ones GitHub marks required
        #    (gh does not expose which are required; stricter is safer).
        failed = [c.name or "?" for c in pr.checks if c.outcome in ("failure", "unknown")]
        pending = [c.name or "?" for c in pr.checks if c.outcome == "pending"]
        if failed:
            return f"PR {url} has failing or inconclusive checks: {', '.join(failed)}"
        if pending:
            raise VerificationError(f"PR {url} has checks still running: {', '.join(pending)}")
        #    ... and the required ones came from the definition the base
        #    branch has, not merely from a run that reported the same name.
        redefined = self._check_definition_problem(pr)
        if redefined:
            return redefined

        # 3. mergeability as computed by GitHub
        if pr.mergeable == "CONFLICTING":
            return f"PR {url} has merge conflicts (mergeable=CONFLICTING)"
        if pr.mergeable != "MERGEABLE":
            raise VerificationError(
                f"GitHub has not determined mergeability of PR {url} "
                f"(mergeable={pr.mergeable or 'unknown'!r})"
            )
        status = pr.merge_state_status
        if status in ("", "UNKNOWN"):
            raise VerificationError(
                f"GitHub reports mergeStateStatus={status or 'unknown'!r} for PR {url}"
            )
        if status not in MERGEABLE_STATE_STATUSES:
            return (
                f"PR {url} is not mergeable right now (mergeStateStatus={status}: "
                f"{MERGE_STATE_HINTS.get(status, 'not accepted by the controller')})"
            )

        # 4. asynchronous merge paths the controller would not own
        if pr.auto_merge_enabled:
            return (
                f"PR {url} already has GitHub auto-merge armed; the controller only performs "
                "synchronous merges of the reviewed HEAD. Disable auto-merge on GitHub"
            )
        with self._reading("the merge-queue status"):
            queue = self.github.get_pr_merge_queue_status(url)
        if queue.in_queue:
            return f"PR {url} is already in a merge queue the controller does not own"
        if queue.enabled:
            return (
                f"the base branch of PR {url} requires a merge queue; `gh pr merge` would "
                "enqueue the PR or arm auto-merge instead of merging the reviewed HEAD "
                "synchronously, which the controller does not allow"
            )
        return ""

    def _protected_path_problem(self, pr: PRInfo) -> str:
        """Refuse to merge unattended a PR that edits the definition of its own checks.

        The green `ci` this gate trusts is produced by the workflow files
        *in the PR*: GitHub runs the PR's version of `.github/workflows/`
        and reports it under the same check name, so a PR that changes them
        also changes what "every check succeeded" means. The controller
        cannot verify that from the outside, and GitHub's own protections
        cannot either while the required check is defined by the branch it
        gates. Such a PR is BLOCKED for a human instead
        (``safety.protected_merge_paths``; an empty list disables the gate).

        Both ends of a rename count: moving a protected file out of the
        protected range removes its content just as an edit would, and the
        listing reports that as one file carrying its former path rather
        than as a deletion.

        Fails closed on a listing GitHub may have truncated: a short file
        list cannot prove a protected path was left alone. GitHubError from
        the read propagates for the caller to classify (transient ->
        bounded re-check, conclusive -> BLOCKED).

        This gates the *definition* of the check, not the trustworthiness of
        a green run: the commands still execute the PR's own code, so a PR
        can weaken what its tests assert without touching a protected path.
        That residual gap is why merge stays behind ``safety.allow_merge``.
        """
        patterns = self.config.safety.protected_merge_paths
        if not patterns:
            return ""
        with self._reading("the changed-file listing"):
            changed = self.github.get_pr_changed_files(pr.url)
        hits = sorted(
            f"{file.previous_path} -> {file.path}" if file.previous_path else file.path
            for file in changed.files
            if any(self.config.safety.protects(path) for path in file.paths)
        )
        if hits:
            shown = ", ".join(hits[:5]) + (", ..." if len(hits) > 5 else "")
            return (
                f"PR {pr.url} changes {shown}: these paths define the hosted checks whose "
                "green result the merge gate trusts, so a green check on this PR is not "
                "independent evidence about it. Review and merge it manually, or narrow "
                "safety.protected_merge_paths"
            )
        if not changed.complete:
            return (
                f"GitHub returned {len(changed.files)} of {changed.total} changed files for "
                f"PR {pr.url}, so the controller cannot prove the PR leaves "
                f"{', '.join(patterns)} untouched"
            )
        return ""

    def _check_definition_problem(self, pr: PRInfo) -> str:
        """Refuse a required check whose green result came from another definition.

        ``_protected_path_problem`` reads the PR's file list; this reads
        GitHub's own record of *what ran*. Each ``safety.required_checks``
        context must resolve, through its details URL, to exactly one GitHub
        Actions run in this repository at the reviewed HEAD, and that run's
        jobs and step names must equal those of the base branch's own most
        recent successful ``push`` run of the same workflow at the base
        branch's current tip. A workflow the PR redefined (also through a
        reusable workflow or an action outside the protected paths), a
        trimmed or extended job, a step whose command changed -- all are a
        named difference and the PR is BLOCKED for a human. The comparison
        is structural: it proves the same definition ran, not that the
        commands it ran assert anything (see ``merge.verification_commands``).

        Conclusive shortfalls (a context absent or duplicated in the rollup,
        not an Actions run, a run at another commit or in another
        repository, no base-branch run to compare against, a short job
        or run listing) return a reason. A base-branch run still in progress raises
        VerificationError (inconclusive); GitHubError from the reads
        propagates for the caller to classify. Disabled by
        ``safety.verify_check_definition: false`` or an empty
        ``safety.required_checks``.
        """
        safety = self.config.safety
        if not safety.verify_check_definition or not safety.required_checks:
            return ""
        state = self._require_state()
        repository = state.repository
        url = pr.url
        if not pr.base_ref:
            return f"PR {url} reports no base branch, so its checks have no reference definition"
        base_tip = ""
        references: dict[int, tuple[int, WorkflowRunJobs]] = {}
        for context in safety.required_checks:
            matches = [check for check in pr.checks if check.name == context]
            if len(matches) != 1:
                return (
                    f"required check {context!r} appears {len(matches)} times in the status "
                    f"rollup of PR {url}, so the controller cannot attribute one workflow run "
                    "to it"
                )
            check = matches[0]
            ref = check.actions_run
            if ref is None:
                return (
                    f"required check {context!r} of PR {url} is not a GitHub Actions check "
                    f"run (details URL: {check.details_url or 'none'}), so the controller "
                    "cannot read back the workflow definition that produced it"
                )
            if ref.repository.lower() != repository.lower():
                return (
                    f"required check {context!r} of PR {url} was produced by a workflow run "
                    f"in {ref.repository}, not in {repository}"
                )
            with self._reading(f"workflow run {ref.run_id} behind required check {context!r}"):
                run = self.github.get_workflow_run(ref.repository, ref.run_id)
            if run.repository.lower() != repository.lower():
                return (
                    f"workflow run {run.id} behind required check {context!r} of PR {url} "
                    f"belongs to {run.repository}, not to {repository}"
                )
            if run.head_sha != pr.head_sha:
                return (
                    f"workflow run {run.id} behind required check {context!r} of PR {url} ran "
                    f"at {run.head_sha[:12]}, not at the reviewed HEAD {pr.head_sha[:12]}"
                )
            if not run.completed:
                raise VerificationError(
                    f"workflow run {run.id} behind required check {context!r} of PR {url} is "
                    f"{run.status}, not completed"
                )
            with self._reading(f"the jobs of workflow run {run.id}"):
                pr_jobs = self.github.get_workflow_run_jobs(ref.repository, run.id)
            if not pr_jobs.complete:
                return (
                    f"GitHub returned {len(pr_jobs.jobs)} of {pr_jobs.total} jobs of workflow "
                    f"run {run.id} behind required check {context!r} of PR {url}, so its "
                    "definition cannot be compared"
                )
            if not base_tip:
                with self._reading(f"the head of base branch {pr.base_ref!r}"):
                    base_tip = self.github.get_branch_head_sha(repository, pr.base_ref)
            cached = references.get(run.workflow_id)
            if cached is None:
                with self._reading(f"the base branch's runs of {run.path}"):
                    runs = self.github.find_workflow_runs(
                        repository,
                        run.workflow_id,
                        branch=pr.base_ref,
                        event="push",
                        head_sha=base_tip,
                    )
                if not runs.complete:
                    return (
                        f"GitHub returned {len(runs.runs)} of {runs.total} push runs of "
                        f"{run.path} on base branch {pr.base_ref!r} at {base_tip[:12]}, so the "
                        f"reference definition for required check {context!r} of PR {url} "
                        "cannot be chosen"
                    )
                # The filter is GitHub's; the facts are re-checked on what
                # came back rather than trusted from the query string.
                candidates = [
                    candidate
                    for candidate in runs.runs
                    if candidate.workflow_id == run.workflow_id
                    and candidate.head_sha == base_tip
                    and candidate.head_branch == pr.base_ref
                    and candidate.event == "push"
                    and candidate.repository.lower() == repository.lower()
                ]
                if not candidates:
                    return (
                        f"base branch {pr.base_ref!r} has no push run of {run.path} at its "
                        f"current tip {base_tip[:12]}, so there is no reference definition to "
                        f"compare required check {context!r} of PR {url} against. The "
                        "workflow must run on pushes to the base branch (or disable "
                        "safety.verify_check_definition)"
                    )
                running = [candidate for candidate in candidates if not candidate.completed]
                if running:
                    raise VerificationError(
                        f"the base branch's own run {running[0].id} of {run.path} at "
                        f"{base_tip[:12]} is still {running[0].status}"
                    )
                reference = max(candidates, key=lambda c: (c.id, c.run_attempt))
                if reference.conclusion != "success":
                    return (
                        f"the base branch's own run {reference.id} of {run.path} at "
                        f"{base_tip[:12]} concluded {reference.conclusion!r}, not success, so "
                        "it cannot serve as the reference definition for required check "
                        f"{context!r} of PR {url}; make {pr.base_ref!r} green first"
                    )
                with self._reading(f"the jobs of the base branch's workflow run {reference.id}"):
                    base_jobs = self.github.get_workflow_run_jobs(repository, reference.id)
                if not base_jobs.complete:
                    return (
                        f"GitHub returned {len(base_jobs.jobs)} of {base_jobs.total} jobs of "
                        f"the base branch's run {reference.id} of {run.path}, so the reference "
                        "definition is incomplete"
                    )
                cached = (reference.id, base_jobs)
                references[run.workflow_id] = cached
            reference_id, base_jobs = cached
            difference = describe_definition_difference(pr_jobs, base_jobs)
            if difference:
                return (
                    f"workflow run {run.id} behind required check {context!r} of PR {url} "
                    f"does not match the base branch's own run {reference_id} of {run.path} "
                    f"at {base_tip[:12]}: {difference}. A green check produced by a different "
                    "definition is not the check the merge gate trusts; review and merge "
                    "manually, or disable safety.verify_check_definition"
                )
        return ""

    def _disarm_async_merge(self, url: str, after: PRInfo) -> tuple[str, bool]:
        """After a merge call that left the PR OPEN, undo any auto-merge it armed.

        Returns ``(note, pending)``: extra text for the outcome message and
        whether a GitHub-side merge may still be pending (auto-merge that
        could not be disabled, PR in the merge queue, or queue status
        unreadable). The controller must never leave such a merge pending
        that could land a later, unreviewed HEAD on its own.
        """
        note = ""
        pending = False
        if after.auto_merge_enabled:
            try:
                self.github.disable_auto_merge(url)
                note += "; auto-merge was armed on the PR and has been disabled again"
            except GitHubError as exc:
                pending = True
                note += (
                    f"; WARNING: auto-merge is armed on the PR and could not be disabled ({exc}) "
                    "— disable it on GitHub immediately"
                )
        try:
            queue = self.github.get_pr_merge_queue_status(url)
        except GitHubError as exc:
            note += f"; merge-queue status could not be read ({exc}) — check on GitHub"
            return note, True
        if queue.in_queue:
            pending = True
            note += "; WARNING: the PR is in the merge queue — remove it on GitHub immediately"
        return note, pending

    def _complete_merge(self, pr: PRInfo, plan: StepPlan, recovered: bool) -> StepOutcome:
        state = self._require_state()
        url = parse_pr_url(pr.url or state.current_pr_url).canonical
        newly = state.record_merge(url)  # idempotent across crash/resume
        state.current_head_sha = pr.head_sha
        # The merge was verified at the reviewed revision (the drift case
        # left through _revision_drift_to_review before anything was counted).
        nxt = self._next_phase(Phase.MERGE, {"head_changed_after_review": False})
        validate_transition(Phase.MERGE, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        how = "already MERGED on GitHub (recovered)" if recovered else "merged by the controller"
        counted = "counted" if newly else "already counted"
        return self._outcome(
            Phase.MERGE,
            plan=plan,
            message=(
                f"PR {url} {how} at reviewed HEAD {pr.head_sha[:12]}; {counted} "
                f"({state.merged_since_epic_update} since last EPIC update); MERGE -> {nxt.value}"
            ),
        )

    def _analyze_plan_notes(self, s: AutoForgeState) -> list[str]:
        """What ANALYZE_EXECUTE would do, from persisted state only (no read, no effect)."""
        effects = s.phase_effects()
        notes = [
            f"would reconcile {record.describe()} ({record.stage.value}, "
            f"{record.attempts} attempt(s)) against GitHub before anything else, and send it "
            "only if GitHub does not already hold it and its attempt bound is left"
            for record in effects.records
        ]
        if isinstance(effects.context, AnalyzeContext):
            notes.append(
                "would complete from the persisted ANALYZE_EXECUTE plan without launching the "
                "agent: push, then open or adopt the PR, then read it back"
            )
            return notes
        branch = self.branch_name_for(s.current_issue_url) if s.current_issue_url else "(none)"
        notes.extend(
            [
                "would first check for an existing open PR carrying the issue's implementation "
                "marker (recovery -> REVIEW without launching the agent)",
                f"would read the default branch head, the head of {branch!r} and the PRs "
                "headed at it, and fetch both heads into the shared object store, before "
                "launching the agent (BLOCKED without launching on a closed or merged PR on "
                "the branch)",
                "would check the agent's reported head_sha against the worktree's detached "
                "HEAD, its ancestry and every published commit message, asking the agent "
                "again on a refusal",
                f"would save the push of that HEAD to {branch!r} and the PR (opened with the "
                "agent's title and body, or the open PR already on the branch adopted), then "
                "push, open or adopt, and read the PR back at exactly the pushed HEAD",
            ]
        )
        return notes

    def _review_plan_notes(self, s: AutoForgeState) -> list[str]:
        """What REVIEW would do, from persisted state only (no read, no effect)."""
        effects = s.phase_effects()
        notes = [
            f"would reconcile {record.describe()} ({record.stage.value}, "
            f"{record.attempts} attempt(s)) against the PR's comments before anything else, "
            "and post it only if no comment carries its marker and its attempt bound is left"
            for record in effects.records
        ]
        if isinstance(effects.context, ReviewContext):
            notes.append(
                f"would complete review round {effects.context.round} from the persisted "
                "REVIEW plan without launching the reviewer: post or reconcile the planned "
                "comment, then re-read the PR's HEAD, base and merge base to judge the round"
            )
            return notes
        notes.extend(
            [
                "REVIEWED_HEAD_SHA, REVIEWED_BASE_REF and REVIEWED_MERGE_BASE_SHA are fetched "
                "from gh immediately before the review",
                "would read the PR's comments first: one carrying this round's marker at the "
                "bound revision, which the controller did not post, blocks without launching",
                "would fetch the bound HEAD and merge base into the shared object store before "
                "launching the reviewer, which publishes nothing",
                "would check the reviewer's round and reviewed_head_sha against the binding and "
                "render the review comment (heading, binding line, findings, the reviewer's "
                "sections, needs-fix line, marker), asking the reviewer again on a refusal",
                "would save the planned comment, post it on the PR itself, and read it back as "
                "the one comment carrying the round's marker",
            ]
        )
        return notes

    def _fix_plan_notes(self, s: AutoForgeState) -> list[str]:
        """What FIX would do, from persisted state only (no read, no effect; #163)."""
        effects = s.phase_effects()
        notes = [
            f"would reconcile {record.describe()} ({record.stage.value}, "
            f"{record.attempts} attempt(s)) against GitHub before anything else, and send it "
            "only if GitHub does not already hold it and its attempt bound is left"
            for record in effects.records
        ]
        if isinstance(effects.context, FixContext):
            notes.append(
                f"would complete FIX of review round {effects.context.round} from the persisted "
                "plan without launching the fixer: create the follow-up issues, append the "
                "markers, push last, then read the follow-ups and the PR head back (a PR head "
                "neither the reviewed HEAD nor the planned candidate routes to REVIEW)"
            )
            return notes
        notes.extend(
            [
                "would re-read the PR's HEAD, base and merge base first: a revision past the "
                "reviewed one routes FIX -> REVIEW without launching the fixer",
                "would read the open issues for each open finding's follow-up marker (handed "
                "to the fixer to reuse) and for earlier rounds' deferrals, and fetch the "
                "reviewed HEAD into the shared object store, before launching the fixer, "
                "which publishes nothing",
                "would check the fixer's resolutions against the open findings and the "
                "follow-up issues handed over, and its head_sha against the worktree's "
                "detached HEAD, its ancestry from the reviewed HEAD and every published commit "
                "message, asking the fixer again on a refusal",
                "would save the plan (new follow-up issues, marker appends to handed-over "
                "issues, then the push of that HEAD as a fast-forward over the reviewed HEAD), "
                "perform it in that order, and read the follow-ups and the PR head back",
            ]
        )
        return notes

    def _update_epic_plan_notes(self, s: AutoForgeState) -> list[str]:
        """What UPDATE_EPIC would do, from persisted state only (no read, no effect)."""
        every = self.config.workflow.epic_update_every
        effects = s.phase_effects()
        notes: list[str] = []
        for record in effects.records:
            notes.append(
                f"would reconcile {record.describe()} ({record.stage.value}, "
                f"{record.attempts} attempt(s)) against the EPIC's comments before anything "
                "else, and post it only if no comment carries its marker and its attempt "
                "bound is left"
            )
        context = effects.context
        if isinstance(context, UpdateEpicContext) and not context.void:
            notes.append(
                "would complete from the persisted UPDATE_EPIC result without launching the "
                "agent: splice the stored roadmap section when merges are pending, then "
                "verify the stored selection "
                f"({context.next_issue_url or 'none: the EPIC is complete'})"
            )
            return notes
        request = self._update_epic_request()
        if request != UpdateEpicRequest.FULL:
            notes.append(
                f"the progress comment is published; the launch is a {request.value} "
                "re-request, whose result may carry no progress text"
            )
        else:
            notes.append(
                "would read the EPIC's comments first: one carrying this issue's progress "
                "marker that the controller did not journal, or two, block without launching"
            )
            notes.append(
                "would post the agent's progress text on the EPIC itself, followed by the "
                f"marker {self._progress_marker() if s.current_pr_url else '(none)'}, after "
                "saving the planned comment, and read it back as the one comment carrying it"
            )
        notes.append(
            "would read the EPIC body and locate its managed roadmap section before "
            "launching the agent (BLOCKED without launching when the markers are ambiguous)"
        )
        if self._roadmap_update_due(s):
            notes.append(
                f"roadmap update due: {s.merged_since_epic_update} merge(s) since the last "
                f"update, workflow.epic_update_every = {every}; would splice the agent's "
                "roadmap_section between the markers, write the EPIC body via "
                "`gh issue edit --body-file`, read it back, require every byte outside "
                "the markers unchanged, and only then reset the merge counter"
            )
        else:
            notes.append(
                f"roadmap update not due: {s.merged_since_epic_update} merge(s) since the "
                f"last update, workflow.epic_update_every = {every}; the EPIC body is not "
                "written and the merge counter is kept (unless the agent reports the EPIC "
                "complete, which requires the final roadmap)"
            )
        return notes

    # -- pre-invocation preparation --------------------------------------------
    def _require_open_pr(self) -> PRInfo:
        state = self._require_state()
        if not state.current_pr_url:
            raise StateError(f"phase {state.phase.value} requires current_pr_url in state")
        pr = self.github.get_pr(state.current_pr_url)
        if parse_pr_url(pr.url or state.current_pr_url).repository.lower() != (
            state.repository.lower()
        ):
            raise VerificationError(f"PR {state.current_pr_url} is not in {state.repository}")
        if not pr.is_open:
            raise VerificationError(
                f"PR {state.current_pr_url} is {pr.state}; the workflow only operates on OPEN PRs"
            )
        if not pr.head_sha:
            raise VerificationError(f"PR {state.current_pr_url} has no readable head SHA")
        return pr

    def _merge_base_of(self, pr: PRInfo) -> str:
        """The merge base of the PR's base branch and its HEAD, as GitHub reports it now.

        This is the commit the PR's diff is computed from. It is read, never
        derived: the base branch's tip is not it (commits landing on the
        base do not move it), and only GitHub knows what the base named
        ``pr.base_ref`` is right now. Raises the read's GitHubError, named,
        for the caller to classify.
        """
        state = self._require_state()
        with self._reading(f"merge base of {pr.base_ref!r} and HEAD {pr.head_sha[:12]}"):
            return self.github.get_merge_base_sha(state.repository, pr.base_ref, pr.head_sha)

    def _bind_review_head(self) -> None:
        """Fetch the real PR HEAD, base and merge base right before the review and persist them.

        The review is a decision about the PR's diff, and the diff is the
        HEAD against the base from their merge base, so all three are bound
        here and all three are re-read after the review
        (:meth:`_apply_review`): a round whose HEAD, base or merge base moved
        while the reviewer worked is stale, not current. A PR whose base or
        merge base cannot be read cannot be bound and is refused before
        anyone is launched.
        """
        state = self._require_state()
        pr = self._require_open_pr()
        if not pr.base_ref:
            raise VerificationError(
                f"PR {state.current_pr_url} has no readable base branch; the review cannot "
                "be bound to the diff it would decide on"
            )
        merge_base = self._merge_base_of(pr)
        state.current_head_sha = pr.head_sha
        state.current_base_ref = pr.base_ref
        state.current_merge_base_sha = merge_base
        state.current_branch = pr.head_ref or state.current_branch
        self._save()

    def _reconcile_review_entry(self, plan: StepPlan) -> StepOutcome | None:
        """Read the PR for this round's comment, and fetch the diff, before the reviewer runs.

        The controller posts the round's review comment itself (K4, #162),
        after it has planned and saved it, and the reviewer publishes
        nothing. So a comment carrying the ``ai-review-result`` marker for
        the upcoming round at the bound HEAD, base and merge base, found
        before any plan was saved, is one the controller did not journal: a
        human's, an agent's that still held a credential, or one posted by
        an older version of the controller's reviewer. It is never adopted
        and never duplicated; the entry blocks naming it (ADR 0004 D9.6,
        D13.5), and only removing the comment or its marker lets the round
        be reviewed. Comments for the same round at another HEAD, against
        another base or from another merge base (an earlier run, a stale
        re-review, a round posted before the PR was retargeted or before its
        base was rewritten) are not this round's and are ignored.

        The reviewer is also told which problems earlier rounds already
        deferred: the open issues carrying this PR's ``ai-follow-up`` marker
        for any finding id (``EXISTING_FOLLOW_UP_ISSUES``). Finding ids are
        round-scoped, so a problem re-raised under a new id would otherwise
        be deferred again into a second issue by the next fixer; the review
        that knows about the first issue does not re-raise it (#90). The
        listing is strict for the same reason the FIX entry's is: "no such
        issue exists" is not knowable from a listing that may be truncated.

        Last, the bound HEAD and merge base are fetched into the shared
        object store, so the reviewer reads the diff with no network git
        of its own. A failed fetch propagates (nothing was launched, and
        'resume' fetches again).
        """
        state = self._require_state()
        self._existing_pr_follow_ups = []
        upcoming = state.review_round + 1
        head = state.current_head_sha.lower()
        base = state.current_base_ref
        merge_base = state.current_merge_base_sha
        pr_ref = parse_pr_url(state.current_pr_url)
        try:
            holder = self._review_comments(pr_ref, upcoming, head, base, merge_base).at_most_one()
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.REVIEW,
                plan,
                f"cannot establish which comment carries the review for round {upcoming} at "
                f"HEAD {head[:12]} on base {base!r} from merge base {merge_base[:12]} of PR "
                f"{pr_ref.canonical}: {exc}. The controller will not review a round whose "
                "comment it could not tell apart from one it would post",
            )
        except ClaimConflictError as exc:
            return self._block(
                Phase.REVIEW,
                plan,
                f"{exc}. The controller posts this round's comment itself and never chooses "
                "between comments it did not post; nothing was launched. Remove the comments or "
                "their markers, then 'unblock'",
            )
        if holder is not None:
            return self._block(Phase.REVIEW, plan, self._unjournaled_review_text(holder.obj.url))
        try:
            deferred = self._follow_up_issues(pr_ref).grouped(
                lambda c: True, f"PR {pr_ref.canonical}"
            )
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            return self._block(
                Phase.REVIEW,
                plan,
                f"cannot establish which follow-up issues already exist for PR "
                f"{pr_ref.canonical}: {exc}. The controller will not launch a reviewer that "
                "could re-raise a problem an earlier round already deferred",
            )
        self._existing_pr_follow_ups = _follow_up_pairs(deferred)
        wanted = [head] + ([merge_base] if merge_base and merge_base != head else [])
        try:
            self._git_transport().fetch(wanted)
        except GitTransportError as exc:
            raise VerificationError(
                f"the controller could not fetch {', '.join(wanted)} into the shared object "
                f"store before launching the reviewer: {exc}. Nothing was launched; 'resume' "
                "fetches again"
            ) from exc
        return None

    def _unjournaled_review_text(self, url: str) -> str:
        """Why a round comment the controller did not journal stops the round (D9.6, D13.5)."""
        state = self._require_state()
        upcoming = state.review_round + 1
        legacy = (
            " It was most likely posted by a reviewer of the previous contract, which "
            "published its own comment before this run was upgraded (ADR 0004 D13.5)."
            if is_legacy_reentry(Phase.REVIEW, state.attempt, state.launch_label)
            else ""
        )
        return (
            f"PR {state.current_pr_url} carries a review comment for round {upcoming} at HEAD "
            f"{state.current_head_sha.lower()[:12]} on base {state.current_base_ref!r} from "
            f"merge base {state.current_merge_base_sha[:12]} that the controller did not "
            f"post: {url}.{legacy} The controller posts this round's comment itself and "
            "journals it before it is sent; it never adopts or duplicates one it did not "
            "journal (ADR 0004 D9.6), and 'unblock' does not change that. Nothing was "
            "launched or posted. Delete that comment or its marker, then 'unblock'"
        )

    def _prepare_fix(self, plan: StepPlan) -> StepOutcome | None:
        """Journal first, then GitHub, before any FIX launch (#163, ADR 0004 §2.5).

        The controller performs this phase's writes itself: it creates the
        follow-up issues the fixer asks for (K5), appends finding markers to
        the follow-up issues it handed over (K6), then pushes the fixer's
        commit to the PR branch (K1, last: the commit point). So:

        - A persisted completion context means the fixer's result was
          accepted and the plan saved. The phase is completed from the
          journal (:meth:`_finish_fix_entry`) and the fixer is never launched
          for it again; a PR head equal to the planned candidate is the
          controller's own push, never a reason to review first.
        - Otherwise the PR is re-read. FIX resolves the findings of one
          review, and those findings are bound to the HEAD that review saw. A
          PR HEAD past it is a push the controller never verified (an
          operator, or a fixer of the previous contract): which findings it
          resolved is not knowable from controller state and is never
          inferred, so the review is stale and the actual HEAD is reviewed
          (``FIX -> REVIEW``, the findings carried to that review) instead of
          a fixer being launched. The findings are bound to the base and the
          merge base too (#95, #96): a retargeted PR or a base rewritten under
          its name is treated the same way; an empty ``reviewed_base_ref`` or
          ``reviewed_merge_base_sha`` (an older state file) has nothing to
          compare and launches the fixer.
        - The open issues are read for the ``ai-follow-up`` marker of (this
          PR, an open finding id): exactly one per finding is handed to the
          fixer (``FOLLOW_UP_ISSUES``) as that finding's follow-up, two or
          more for one finding is a state the controller cannot resolve
          without choosing, and a listing that cannot be proven complete
          blocks, because "no such issue exists" is then not knowable. The
          same listing gives the earlier rounds' deferrals
          (``EXISTING_FOLLOW_UP_ISSUES``, #90).
        - The observation records the PR branch at the reviewed HEAD and,
          per open finding, its follow-up issue or none. A re-entry after a
          launch of this entry (a correction relaunch, or a 'resume' after
          the fixer failed) honors it: a follow-up issue that appeared, or
          disappeared, since is not the controller's and blocks, never
          adopted (ADR 0004 §2.5). The legacy re-entry of a run upgraded
          while a fixer of the previous contract was publishing (D13.6) has
          no observation to honor: its follow-ups are handed over as found.

        The reviewed HEAD is fetched into the shared object store before the
        launch (objects only; no ref moves), so the fixer starts from it
        without contacting the remote. Only an unavailable GitHub and a
        failed fetch propagate (nothing was launched and 'resume' reads
        again); a conclusive failure blocks.
        """
        state = self._require_state()
        self._existing_follow_ups = {}
        self._existing_pr_follow_ups = []
        self._fix_candidate = ""
        effects = state.phase_effects()
        if isinstance(effects.context, FixContext):
            return self._finish_fix_entry(plan)
        if not state.open_findings:
            raise StateError("FIX phase entered without open findings in state")
        pr = self._require_open_pr()
        reviewed = state.reviewed_head_sha.lower()
        reviewed_base = state.reviewed_base_ref
        if pr.head_sha.lower() != reviewed:
            state.last_fix_resolutions = []
            return self._revision_drift_to_review(
                Phase.FIX,
                plan,
                pr,
                message=(
                    f"PR HEAD {pr.head_sha[:12]} is past the reviewed HEAD {reviewed[:12]} "
                    f"that the open findings of round {state.review_round} are bound to "
                    "(a push the controller did not make; it does not infer which findings it "
                    "resolved); FIX -> REVIEW of the actual HEAD, no fixer launched"
                ),
            )
        if reviewed_base and not pr.base_ref:
            # Unreadable, not retargeted: the same refusal as the review and
            # merge entries, never a guess either way.
            raise VerificationError(
                f"PR {state.current_pr_url} has no readable base branch, so the open findings "
                f"of round {state.review_round} (bound to base {reviewed_base!r}) cannot be "
                "confirmed as findings of this PR's diff"
            )
        if reviewed_base and pr.base_ref != reviewed_base:
            # Same commits, another base: the findings describe a diff the PR
            # no longer proposes. Same rule as HEAD drift (#95).
            state.last_fix_resolutions = []
            return self._revision_drift_to_review(
                Phase.FIX,
                plan,
                pr,
                message=(
                    f"PR base changed to {pr.base_ref!r} from the reviewed base "
                    f"{reviewed_base!r} that the open findings of round {state.review_round} "
                    "are bound to (the findings describe the diff against the old base); "
                    "FIX -> REVIEW of the actual revision, no fixer launched"
                ),
            )
        reviewed_merge_base = state.reviewed_merge_base_sha
        if reviewed_merge_base:
            merge_base = self._merge_base_of(pr)
            if merge_base != reviewed_merge_base:
                # Same commits, same base name, another base history: the
                # findings describe a diff the PR no longer shows (#96).
                state.last_fix_resolutions = []
                return self._revision_drift_to_review(
                    Phase.FIX,
                    plan,
                    pr,
                    message=(
                        f"PR merge base moved to {merge_base[:12]} from the reviewed merge base "
                        f"{reviewed_merge_base[:12]} that the open findings of round "
                        f"{state.review_round} are bound to (base {pr.base_ref!r} was rewritten "
                        "under its name; the findings describe the diff from the old merge "
                        "base); FIX -> REVIEW of the actual revision, no fixer launched"
                    ),
                    merge_base_sha=merge_base,
                )
        problem = self._fix_branch_problem(pr)
        if problem:
            return self._block(Phase.FIX, plan, problem + ". Nothing was launched")
        pr_ref = parse_pr_url(state.current_pr_url)
        open_ids = [str(f["id"]) for f in state.open_findings]
        try:
            # One listing serves both: the open findings' own follow-ups
            # (checked below) and the earlier rounds' deferrals, handed to
            # the fixer so a re-raised problem is recorded on the issue that
            # already exists instead of in a second one (#90).
            own, deferred = self._fix_follow_ups(pr_ref, open_ids)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.FIX,
                plan,
                f"cannot establish which follow-up issues already exist for the open "
                f"findings of PR {pr_ref.canonical}: {exc}. The controller will not launch a "
                "fixer whose deferrals could create a second one",
            )
        except ClaimConflictError as exc:
            return self._block(
                Phase.FIX,
                plan,
                f"{exc}. The controller never chooses between them: close or repair the "
                "extra or unreadable issue(s) so exactly one remains, then start a new run",
            )
        ref = f"refs/heads/{pr.head_ref}"
        observed = self._fix_observed_objects(pr_ref, open_ids, own)
        observation = effects.observation
        honored = (
            observation
            if observation is not None
            and state.attempt >= 1
            and not is_legacy_reentry(Phase.FIX, state.attempt, state.launch_label)
            else None
        )
        if honored is not None and (
            dict(honored.objects) != observed or dict(honored.refs) != {ref: reviewed}
        ):
            return self._block(
                Phase.FIX, plan, self._unjournaled_fix_text(honored, observed, ref, reviewed)
            )
        try:
            self._git_transport().fetch([reviewed])
        except GitTransportError as exc:
            raise VerificationError(
                f"the controller could not fetch the reviewed HEAD {reviewed} into the shared "
                f"object store before launching the fixer: {exc}. Nothing was launched; "
                "'resume' fetches again"
            ) from exc
        if honored is None:
            # D4.4: persisted before the launch, so that a follow-up issue a
            # later entry finds is explained by this read or not at all.
            state.entry_observation = EntryObservation(
                Phase.FIX,
                state.current_issue_url,
                state.current_pr_url,
                {ref: reviewed},
                reviewed,
                observed,
            ).to_dict()
        self._existing_follow_ups = own
        self._existing_pr_follow_ups = deferred
        state.current_head_sha = pr.head_sha
        self._save()
        return None

    def _fix_branch_problem(self, pr: PRInfo) -> str:
        """Why the controller cannot push a fix to ``pr``'s branch, or "" when it can."""
        state = self._require_state()
        url = parse_pr_url(pr.url or state.current_pr_url).canonical
        if pr.head_repository and pr.head_repository.lower() != state.repository.lower():
            return (
                f"PR {url} is headed in {pr.head_repository}, not in {state.repository}: the "
                "controller pushes a fix to a branch of the run's repository only"
            )
        if not pr.head_ref or not valid_branch_name(pr.head_ref):
            return (
                f"PR {url} has no readable head branch ({pr.head_ref!r}), so the controller "
                "has no branch to push the fix to"
            )
        return ""

    def _fix_follow_ups(
        self, pr_ref: GitHubPullRequestRef, open_ids: list[str]
    ) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """One open-issue listing: each open finding's follow-up, and the earlier deferrals.

        ``({finding id: issue URL}, [(finding id, issue URL), ...])``. Raises
        what the listing raises: two issues for one finding, or a defect,
        is a :class:`ClaimConflictError`.
        """
        follow_ups = self._follow_up_issues(pr_ref)
        own: dict[str, str] = {}
        for fid in open_ids:
            holder = follow_ups.claimants(
                (pr_ref.identity, fid), _finding_what(pr_ref, fid)
            ).at_most_one()
            if holder is not None:
                own[fid] = parse_issue_url(holder.obj.url).canonical
        deferred = follow_ups.grouped(
            lambda c: c.finding_id not in open_ids, f"PR {pr_ref.canonical}"
        )
        return own, _follow_up_pairs(deferred)

    @staticmethod
    def _fix_observed_objects(
        pr_ref: GitHubPullRequestRef, open_ids: list[str], own: dict[str, str]
    ) -> dict[str, str | None]:
        """The entry observation's objects: each open finding's marker, and its issue or None."""
        return {render_follow_up_marker(pr_ref.canonical, fid): own.get(fid) for fid in open_ids}

    def _unjournaled_fix_text(
        self,
        recorded: EntryObservation,
        observed: dict[str, str | None],
        ref: str,
        reviewed: str,
    ) -> str:
        state = self._require_state()
        changes = []
        for marker in sorted(set(recorded.objects) | set(observed)):
            before, now = recorded.objects.get(marker), observed.get(marker)
            if before != now:
                fid = scan(FOLLOW_UP, marker).claims[0].finding_id
                changes.append(
                    f"the follow-up issue of {fid} is {now or 'none'}, but this phase's entry "
                    f"recorded {before or 'none'}"
                )
        if dict(recorded.refs) != {ref: reviewed}:
            changes.append(
                f"the PR branch is {ref} at {reviewed}, but this phase's entry recorded "
                f"{', '.join(f'{r} at {s}' for r, s in sorted(recorded.refs.items())) or 'none'}"
            )
        return (
            f"{'; '.join(changes)}. Something other than the controller created, closed or "
            f"marked a follow-up issue of PR {state.current_pr_url} after the fixer was "
            "launched, and the controller creates the follow-up issues itself, from a plan it "
            "journals before anything is sent; it never adopts one it did not journal (ADR "
            "0004 §2.5). Nothing was created, appended or pushed. If such an issue is the "
            "finding's follow-up, 'unblock' starts a fresh entry that hands it to the fixer; "
            "otherwise close it or remove its marker first"
        )

    # -- durable-claim reads --------------------------------------------------
    #
    # One read per marker kind, shared by the phase entry and the post-agent
    # read-back, so both consume the same validated identity model. Listings
    # are strict: a set the client cannot prove complete, or a row it cannot
    # decode as the object asked for, raises GitHubError. A defective marker
    # anywhere in the set raises ClaimConflictError when a cardinality
    # question is asked (``autoforge.claims``). Every consumer applies one
    # rule to what it gets: GitHubUnavailableError is transient and
    # propagates; a conclusive GitHubError or a ClaimConflictError blocks an
    # entry without launching and rejects a read-back (VerificationError).

    def _follow_up_issues(
        self, pr_ref: GitHubPullRequestRef
    ) -> Collection[IssueInfo, FollowUpClaim]:
        """Every follow-up claim naming ``pr_ref`` across the repository's open issues.

        Claims for another PR are not this PR's business and are dropped; a
        defect on any open issue still counts, whatever PR it names, because
        the object carrying it was not readable.
        """
        state = self._require_state()
        issues = self.github.list_open_issues(state.repository, strict=True)
        whole = collect(FOLLOW_UP, issues, "open issue")
        mine = tuple(h for h in whole.holders if h.claim.pr.identity == pr_ref.identity)
        return Collection(whole.kind, whole.noun, mine, whole.defects)

    def _implementation_prs(self, issue: GitHubIssueRef) -> Claimants[PRInfo, ImplementationClaim]:
        """The open PRs claiming to implement ``issue``, from one complete listing."""
        state = self._require_state()
        prs = self.github.list_open_prs(state.repository)
        return collect(IMPLEMENTATION, prs, "open PR").claimants(
            issue.identity, f"issue {issue.canonical}"
        )

    def _review_comments(
        self, pr_ref: GitHubPullRequestRef, round_: int, head: str, base: str, merge_base: str
    ) -> Claimants[CommentInfo, ReviewClaim]:
        """The PR's comments claiming review ``round_`` of the revision (HEAD, base, merge base).

        The key is the revision the round decided on, HEAD, base *and*
        merge base: a comment carrying the round's marker for the same HEAD
        against another base (posted before the PR was retargeted) or from
        another merge base (posted before the base was rewritten under its
        name, #96), or for no base or merge base at all (written before the
        marker recorded them), is not this round's and matches nothing, so
        it is neither adopted at entry nor accepted on read-back.
        """
        if not base:
            raise StateError(
                f"review round {round_} of PR {pr_ref.canonical} has no bound base branch"
            )
        if not merge_base:
            raise StateError(
                f"review round {round_} of PR {pr_ref.canonical} has no bound merge base"
            )
        comments = self.github.get_pr_comments(pr_ref.canonical)
        return collect(REVIEW, comments, "comment").claimants(
            (round_, head.lower(), base, merge_base.lower()),
            f"round {round_} at HEAD {head[:12]} on base {base!r} from merge base "
            f"{merge_base[:12]} of PR {pr_ref.canonical}",
        )

    def _progress_comments(self) -> Claimants[CommentInfo, ProgressClaim]:
        """The EPIC comments claiming this entry's (finished issue, merged PR)."""
        state = self._require_state()
        if not state.current_pr_url:
            raise StateError("UPDATE_EPIC entered without the merged PR in state")
        issue = parse_issue_url(state.current_issue_url)
        pr = parse_pr_url(state.current_pr_url)
        comments = self.github.get_issue_comments(state.epic_url)
        return collect(PROGRESS, comments, "comment").claimants(
            (issue.identity, pr.identity),
            f"issue {issue.canonical} (PR {pr.canonical}) on EPIC {state.epic_url}",
        )

    def _reconcile_update_epic_entry(self, plan: StepPlan) -> StepOutcome | None:
        """Journal first, then GitHub, before any UPDATE_EPIC launch (ADR 0004 §2.10).

        The controller posts the phase's progress comment itself (K8) and
        writes the EPIC's roadmap section; the agent publishes nothing. So:

        - A persisted completion context means the agent's result was
          accepted. It is completed from the journal (the K8 record
          reconciled, the section spliced, the selection verified) and the
          agent is never relaunched for it, unless a persisted rejection
          voided one of its inputs: then the entry reads the EPIC body and
          the launch is a re-request for that input only (D4.7).
        - Otherwise the EPIC's comments are read. None carrying the marker of
          (finished issue, merged PR) is the normal case. One that the
          controller did not journal blocks (D9.7): it is never adopted and
          never duplicated. The two exceptions are a comment this entry
          already adopted (its URL is in the persisted entry observation) and
          the one-shot legacy re-entry of a run upgraded while an agent of the
          previous contract was publishing (D13.3, D13.7), whose comment is
          adopted and whose launch asks for the selection only. Two or more
          block without launching anyone.

        The EPIC body is read and split around its managed roadmap section
        before every launch: the agent sees the section as
        ``CURRENT_ROADMAP_SECTION``, and the completion context stores the
        digests of the bytes outside it. A body whose markers cannot be read
        unambiguously, or that cannot be read for a conclusive reason, blocks
        before anyone is launched; only an *unavailable* GitHub propagates,
        as the transient failure it is.
        """
        state = self._require_state()
        self._adopted_progress_comment_url = ""
        self._epic_roadmap_at_entry = None
        context = self._update_epic_context()
        if context is not None:
            if not context.void:
                return self._finish_update_epic_entry(plan)
            reason = self._drive_progress_records()
            if reason:
                return self._block(Phase.UPDATE_EPIC, plan, reason)
            return self._read_entry_roadmap(plan)
        marker = self._progress_marker()
        try:
            holder = self._progress_comments().at_most_one()
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.UPDATE_EPIC,
                plan,
                f"cannot establish which comment on EPIC {state.epic_url} is the progress "
                f"comment for issue {state.current_issue_url}: {exc}. The controller will "
                "not launch an agent before it knows whether the comment exists",
            )
        except ClaimConflictError as exc:
            return self._block(
                Phase.UPDATE_EPIC,
                plan,
                f"{exc}. The controller never chooses between them: remove or repair the "
                "extra or unreadable comment(s) so that at most one remains, then start a new "
                "run",
            )
        observation = state.phase_effects().observation
        observed = observation.objects.get(marker) if observation is not None else None
        if holder is not None:
            journaled = observed is not None and observed == holder.obj.url
            legacy = observation is None and is_legacy_reentry(
                Phase.UPDATE_EPIC, state.attempt, state.launch_label
            )
            if not (journaled or legacy):
                return self._block(
                    Phase.UPDATE_EPIC, plan, self._unjournaled_progress_text(holder.obj.url)
                )
            self._adopted_progress_comment_url = holder.obj.url
        elif observed is not None:
            return self._block(
                Phase.UPDATE_EPIC,
                plan,
                f"the progress comment {observed} that this phase adopted is no longer on EPIC "
                f"{state.epic_url}. The controller does not post a replacement for a comment it "
                "did not write; restore it or post the progress report by hand, then start a "
                "new run",
            )
        blocked = self._read_entry_roadmap(plan)
        if blocked is not None:
            return blocked
        if observation is None:
            # D4.4: persisted by the pre-launch save, so that a comment found
            # by a later entry is explained by this read or not at all.
            state.entry_observation = EntryObservation(
                Phase.UPDATE_EPIC,
                state.current_issue_url,
                state.current_pr_url,
                {},
                None,
                {marker: holder.obj.url if holder is not None else None},
            ).to_dict()
        return None

    def _unjournaled_progress_text(self, url: str) -> str:
        state = self._require_state()
        return (
            f"EPIC {state.epic_url} carries a progress comment for issue "
            f"{state.current_issue_url} (PR {state.current_pr_url}) that the controller did "
            f"not post: {url}. The controller posts this comment itself and journals it before "
            "it is sent; it never adopts or duplicates one it did not journal (ADR 0004 "
            "D9.7). Nothing was posted. Delete that comment (the controller then posts its "
            "own), or finish the EPIC update by hand and start a new run"
        )

    def _read_entry_roadmap(self, plan: StepPlan) -> StepOutcome | None:
        """Read the EPIC body before a launch; block on a conclusive failure."""
        state = self._require_state()
        try:
            self._epic_roadmap_at_entry = self._read_epic_roadmap()
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.UPDATE_EPIC,
                plan,
                f"the body of EPIC {state.epic_url} could not be read: {exc}. This is not a "
                "transient GitHub failure (authentication, permissions, or malformed data), "
                "so the controller will not launch an agent whose roadmap section it could "
                "not splice into a body it has not read; nothing was written. Fix the cause, "
                "then 'resume'",
            )
        except RoadmapError as exc:
            return self._block(
                Phase.UPDATE_EPIC,
                plan,
                f"EPIC {state.epic_url}: {exc}. The controller edits only the text between "
                "the markers and will not guess which text that is: repair the EPIC body so "
                "it carries one section (or none), then 'resume'",
            )
        return None

    def _finish_update_epic_entry(self, plan: StepPlan) -> StepOutcome:
        """Complete UPDATE_EPIC from its persisted context, launching nothing (§2.10)."""
        state = self._require_state()
        try:
            nxt, message = self._complete_update_epic()
        except VerificationError:
            # The rejection that voided an input is persisted before the
            # step ends, so that 'resume' re-requests it (D4.7).
            self._save()
            raise
        if nxt == Phase.BLOCKED:
            return self._block(Phase.UPDATE_EPIC, plan, message)
        validate_transition(Phase.UPDATE_EPIC, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.UPDATE_EPIC,
            plan=plan,
            message=f"{message} (completed from the persisted UPDATE_EPIC result; no agent "
            "launched)",
        )

    def _read_epic_roadmap(self) -> RoadmapSplit:
        """The EPIC body as GitHub holds it now, split around its managed section."""
        state = self._require_state()
        return split_roadmap(self.github.get_issue(state.epic_url).body)

    # ======================================================================
    # REPLAN_REEXECUTE
    #
    # One transaction, one owner per operation. ``autoforge.replan_txn`` owns
    # the lifecycle and every acceptance predicate; the methods below are the
    # GitHub I/O and persistence around them, so the normal path and the
    # crash-recovery path cannot disagree about what is acceptable.
    #
    #   policy decision        REVIEW      ``evaluate_replan_policy``
    #   transaction creation   controller  ``_prepare_replan``
    #   agent invocation       controller  ``_invoke_phase``
    #   replacement PR         agent       (the only agent-owned write here)
    #   candidate discovery    controller  ``_bind_replacement``
    #   verification           shared      ``replan_txn`` predicates
    #   supersede / close      controller  ``_supersede_source`` (the only
    #                                      caller of ``close_pr`` in this phase)
    #   close ownership        controller  ``_close_not_ours`` (receipt)
    #   compensation           controller  ``_compensate_close`` (durable),
    #                                      carried out by ``_run_compensation``
    #   activation             controller  ``_activate_if_verified``
    #   recovery               controller  ``_drive_replan`` (replays intent)
    #   rejection/escalation   controller  ``_reject_replan`` (persisted)
    #
    # The agent never closes, merges or adopts a PR; the controller never
    # writes code.
    # ======================================================================

    def _replan_log_metadata(self) -> dict:
        """Run-log metadata for a REPLAN_REEXECUTE invocation."""
        txn = ReplanTransaction.from_dict(self._require_state().replan_transaction)
        return {
            "transaction_id": txn.transaction_id,
            "stage": txn.stage.value,
            "execution_attempt": txn.expected_execution_attempt,
            "escalation_count": self._require_state().escalation_count,
            "trigger": txn.escalation.get("trigger", ""),
            "recent_finding_counts": txn.escalation.get("recent_finding_counts", []),
            "previous_pr_url": txn.source_pr_url,
            "historical_finding_count": txn.evidence_finding_count,
            "preexisting_pr_urls": txn.preexisting_pr_urls,
        }

    def _save_replan_txn(self, txn: ReplanTransaction) -> None:
        self._require_state().replan_transaction = txn.to_dict()
        self._save()

    def _replan_block_text(self, txn: ReplanTransaction, reason: str) -> str:
        """BLOCKED text that always says what happened to the source PR."""
        if txn.journal_defects:
            # The journal could not be read in full, so it cannot say whether
            # the close it may have recorded was performed. Never claim that
            # nothing happened on the strength of fields that fell back to
            # their defaults.
            tail = (
                "The persisted transaction is unreadable, so whether the source PR "
                f"{txn.source_pr_url or txn.decision_pr_url or '(unknown)'} was already closed "
                "by it cannot be "
                "determined from local state; check GitHub before repairing the journal"
            )
        elif txn.stage in CLOSE_BEGUN_STAGES or txn.superseded_at:
            tail = (
                f"This transaction had already begun closing the source PR {txn.source_pr_url}, "
                f"and the replacement {txn.replacement_pr_url or '(none)'} was not activated"
            )
        else:
            # Pre-close stages: the controller performed no destructive write,
            # which is all it can vouch for. Whether the source is *open* is a
            # GitHub fact that may have changed under a human's hand since it
            # was last read, so it is not asserted here.
            tail = (
                f"This transaction did not close PR "
                f"{txn.source_pr_url or txn.decision_pr_url or '(none)'}, which keeps its "
                "findings; nothing was closed or merged by the controller"
            )
        return f"cannot safely REPLAN_REEXECUTE: {reason}. {tail}. A human must decide next."

    def _reject_replan(self, txn: ReplanTransaction, reason: str, pr_url: str = "") -> StepOutcome:
        """Persist a conclusive refusal, then enter BLOCKED.

        Rejection is monotonic and durable: it is written into the transaction
        *before* the phase is blocked, so a later ``resume`` replays this
        decision instead of re-deriving one from GitHub facts that cannot
        encode it (a replacement whose tests failed still looks like a
        perfectly ordinary open, issue-linked PR). Ambiguity is refused the
        same way — the controller must never guess which candidate is the
        replacement.
        """
        text = self._replan_block_text(txn, reason)
        txn.stage = ReplanStage.REJECTED
        txn.rejection_reason = reason
        if pr_url:
            txn.rejected_pr_url = pr_url
        self._require_state().replan_transaction = txn.to_dict()
        return self._block(Phase.REPLAN_REEXECUTE, None, text)

    def _drive_replan(self) -> StepOutcome | None:
        """Advance the replan transaction as far as it goes without an agent.

        Single entry point for both a fresh REPLAN_REEXECUTE step and a
        ``resume`` after a crash: there is no separate recovery policy to drift
        from the normal one. Returns an outcome once the phase is resolved (the
        replacement became active, or the transaction was refused), or ``None``
        when the replacement agent still has to be invoked.
        """
        state = self._require_state()
        if not state.replan_transaction:
            raise StateError("REPLAN_REEXECUTE entered without a replan transaction in state")
        try:
            txn = ReplanTransaction.from_dict(state.replan_transaction)
        except ValueError as exc:
            return self._block(
                Phase.REPLAN_REEXECUTE, None, f"persisted replan transaction is unusable: {exc}"
            )
        if txn.stage is ReplanStage.REJECTED:
            # Replay, never launder: the decision was already made and saved.
            return self._block(
                Phase.REPLAN_REEXECUTE,
                None,
                self._replan_block_text(txn, txn.rejection_reason or "refused by verification"),
            )
        # Before any stage reads or writes the source -- the compensation and
        # the activation included -- the journal's source must be the PR this
        # run holds. A well-formed journal about some other PR is not a
        # checkpoint the run may act on, whatever GitHub says about that PR.
        unbound = self._replan_unbound(txn)
        if unbound is not None:
            return unbound
        if txn.stage is ReplanStage.COMPENSATING:
            # The undo was decided and persisted before the reopen was
            # attempted. Replay it; never fall through to the supersede step.
            return self._run_compensation(txn)
        if txn.stage is ReplanStage.PENDING:
            prepared = self._prepare_replan(txn)
            if prepared is not None:
                return prepared
        if txn.stage is ReplanStage.PREPARED:
            # A PR bound to this transaction can only exist if the agent ran:
            # the id is random, controller-generated and persisted before the
            # invocation. So "crashed before invoking" and "crashed while the
            # agent ran" are one recoverable state, resolved by looking.
            refused = self._bind_replacement(txn)
            if refused is not None:
                return refused
            if not txn.is_bound:
                return None  # nothing exists yet -> invoke the replan agent
        return self._supersede_source(txn)

    def _replan_unbound(self, txn: ReplanTransaction) -> StepOutcome | None:
        """Refuse a transaction whose source or issue is not this run's.

        :func:`verify_run_binding` is the rule; this persists its refusal so
        ``resume`` replays it. Called at the entry of :meth:`_drive_replan`,
        which every stage passes through, and again by
        :meth:`_supersede_source` immediately before the destructive write,
        which the post-agent path reaches without re-entering the reducer.
        """
        state = self._require_state()
        reason = verify_run_binding(
            txn, state.repository, state.current_pr_url, state.current_issue_url
        )
        if reason:
            return self._reject_replan(txn, reason)
        return None

    def _source_merge_base(self, pr: PRInfo) -> tuple[str, str]:
        """The source PR's merge base as GitHub reports it now, or why it could not be read.

        The merge base is not a fact of the PR object: it is a second read,
        for the PR's base and HEAD as just read (#96). ``("", "")`` when the
        PR has no readable base or HEAD, because there is no pair to ask
        about and the source verifier refuses the missing half first with
        the better message. A conclusive read failure is returned as text
        for the caller to classify at its stage -- a refusal before the
        close, a compensated drift after it, a terminal block at activation
        -- and a transient one propagates, so the stage stays resumable.
        """
        if not pr.base_ref or not pr.head_sha:
            return "", ""
        try:
            return self._merge_base_of(pr), ""
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return "", str(exc)

    def _prepare_replan(self, txn: ReplanTransaction) -> StepOutcome | None:
        """Checkpoint every fact the replan decision rests on, before invoking.

        This is the last point at which a replan costs nothing to refuse, and
        the first at which the controller commits. It fixes the source PR and
        its exact HEAD, the independently verified base branch, the complete
        review evidence the replacement must answer for, the set of PRs that
        already exist (which therefore can never *be* the replacement), and the
        random transaction id that is the only accepted proof of causality.
        """
        state = self._require_state()
        truncated = truncated_evidence_rounds(state.review_history)
        if truncated:
            # ``evaluate_replan_policy`` refuses this too, but REPLAN_REEXECUTE
            # is also reachable by ``resume``, and this is the last checkpoint
            # before an agent may open a replacement PR.
            return self._reject_replan(
                txn,
                "the persisted findings of review round(s) "
                f"{', '.join(str(r) for r in truncated)} are an incomplete copy of the review, so "
                "the replacement could not be required to consider every actionable finding",
            )
        if not state.current_pr_url:
            raise StateError("REPLAN_REEXECUTE requires current_pr_url in state")
        # ``txn.issue_url`` is not taken from ``state.current_issue_url`` here:
        # REVIEW recorded it with the decision and ``_replan_unbound`` has
        # already bound it to the run, so the prepare step re-derives nothing
        # a substituted state could redirect.
        try:
            source = self.github.get_pr(state.current_pr_url)
            repo = self.github.get_repo(state.repository)
            # Repository-wide and complete, for the same reason the candidate
            # lookup is: an issue-shaped filter cannot see a PR that is not
            # (yet) linked to the issue.
            preexisting = self.github.list_open_prs(state.repository)
            # Read *before* the transaction id is generated below, so every PR
            # that could already be carrying a copied marker is under it.
            watermark = self.github.latest_pr_number(state.repository)
        except GitHubUnavailableError:
            raise  # unknown, not refused: `resume` re-reads
        except GitHubError as exc:
            return self._reject_replan(txn, f"cannot checkpoint the replan source: {exc}")
        try:
            # URLs read back from GitHub are data, not proof of shape: a
            # malformed one is a conclusive refusal, not a crash.
            source_ref = parse_pr_url(source.url or state.current_pr_url)
            preexisting_urls = sorted(
                {parse_pr_url(pr.url).canonical for pr in preexisting if pr.url}
            )
        except ConfigurationError as exc:
            return self._reject_replan(txn, f"cannot checkpoint the replan source: {exc}")
        if source_ref.repository.lower() != state.repository.lower():
            return self._reject_replan(
                txn, f"PR {source_ref.canonical} is not in {state.repository}"
            )
        if not source.is_open or not source.head_sha or not source.head_ref or not source.base_ref:
            # The branch and the base are checkpointed alongside the HEAD and
            # enforced by every later source comparison; an empty one would
            # enforce nothing, so it is refused exactly like an unreadable
            # HEAD.
            return self._reject_replan(
                txn,
                f"PR {source_ref.canonical} is {source.state or '(unknown)'} with HEAD "
                f"{source.head_sha or '(unreadable)'} on branch "
                f"{source.head_ref or '(unreadable)'} against base "
                f"{source.base_ref or '(unreadable)'}; only an OPEN PR at a readable HEAD, "
                "branch and base can be superseded",
            )
        if not repo.default_branch:
            return self._reject_replan(
                txn, f"repository {state.repository} has no readable default branch"
            )
        if watermark < source_ref.number:
            return self._reject_replan(
                txn,
                f"repository {state.repository} reports {watermark} as its latest pull-request "
                f"number, which cannot be right while PR #{source_ref.number} exists",
            )
        if not any(same_pr_url(source_ref.canonical, url) for url in preexisting_urls):
            # The source was just read as OPEN, so a consistent listing holds
            # it; one that does not was taken after the source moved. The
            # snapshot is checkpointed as the set of PRs that can never be the
            # replacement, and a journal is refused on load when it is empty,
            # so it must be proven to contain the source before it is written.
            return self._reject_replan(
                txn,
                f"PR {source_ref.canonical} was read as OPEN but is missing from the open "
                f"pull-request listing of {state.repository}; the source moved between reads "
                "and cannot be checkpointed",
            )
        # The merge base is the last part of the diff the decision was made
        # on (#96): read for the base and HEAD just read, checkpointed with
        # them, and enforced by every later source comparison. An unreadable
        # one is refused like an unreadable HEAD: a checkpoint that cannot
        # prove which diff it holds must not be written.
        merge_base, unread = self._source_merge_base(source)
        if unread:
            return self._reject_replan(txn, f"cannot checkpoint the replan source: {unread}")
        drift = verify_decision_point(source, txn, merge_base)
        if drift:
            return self._reject_replan(txn, drift)
        try:
            history = HistoricalReviewCollector(self.github).collect(
                state.current_pr_url, state.review_history, state.verification_failures
            )
        except GitHubUnavailableError:
            raise  # the evidence is unread, not incomplete: `resume` re-reads
        except GitHubError as exc:
            return self._reject_replan(
                txn, f"cannot collect the review evidence the replacement must answer for: {exc}"
            )
        if history.recorded_finding_count < 1:
            # A replan is decided only by a review that ended with findings,
            # and those findings are what the replacement must acknowledge.
            # Evidence that collects to nothing is a history no review wrote
            # (a hand edit), not a replan with nothing to answer for: the
            # acknowledgement requirement would be vacuous, and the journal
            # would be refused on load as incomplete anyway.
            return self._reject_replan(
                txn,
                f"the persisted review history of PR {source_ref.canonical} records no "
                "actionable finding, so there is no evidence a replacement could be required "
                "to answer for",
            )
        txn.transaction_id = new_transaction_id()
        txn.stage = ReplanStage.PREPARED
        txn.source_pr_url = source_ref.canonical
        txn.source_branch = state.current_branch or source.head_ref
        txn.source_head_sha = source.head_sha
        txn.source_base_ref = source.base_ref
        txn.source_merge_base_sha = merge_base
        txn.source_review_round = state.review_round
        txn.base_branch = repo.default_branch
        txn.evidence_finding_count = history.recorded_finding_count
        txn.rendered_findings = history.render_findings()
        txn.rendered_observations = history.render_observations()
        txn.rendered_verification_failures = history.render_verification_failures()
        txn.preexisting_pr_urls = preexisting_urls
        txn.pr_number_watermark = watermark
        txn.expected_execution_attempt = state.execution_attempt + 1
        self._save_replan_txn(txn)
        return None

    def _bind_replacement(
        self, txn: ReplanTransaction, claimed_url: str = ""
    ) -> StepOutcome | None:
        """Find, verify and checkpoint the PR causally bound to ``txn``.

        Returns a BLOCKED outcome when the candidate set is conclusively
        unusable; otherwise ``None``, with ``txn.is_bound`` telling the caller
        whether a replacement was found. ``claimed_url`` is the agent's
        CONTROL_RESULT claim: it never *selects* the candidate, it is only
        checked against the one GitHub proves.
        """
        state = self._require_state()
        try:
            # Every open PR in the repository, to the end: "no candidate exists"
            # decides whether the agent runs again, so it must be neither a
            # truncated listing in disguise nor a filtered one. A replacement
            # the agent created but had not yet linked to the issue is exactly
            # what an issue-shaped filter drops, and re-running the agent on it
            # is the one outcome crash idempotency forbids.
            open_prs = self.github.list_open_prs(state.repository)
        except GitHubUnavailableError:
            raise  # the candidate set is unknown, not ambiguous: stay resumable
        except GitHubError as exc:
            return self._reject_replan(txn, f"cannot list replacement PR candidates: {exc}")
        selection = select_bound_candidate(open_prs, txn)
        if selection.disposition is Disposition.NONE:
            # Open listing says "none" -- but a replacement the first agent
            # created and that was then closed is invisible to it. Treating
            # that as absence would invoke the agent a second time, so an
            # exhaustive all-states listing is consulted before concluding
            # nothing exists. A marker-bearing non-open claimant is a durable
            # rejection naming the PR, never absence.
            try:
                all_prs = self.github.list_all_prs(state.repository, strict=True)
            except GitHubUnavailableError:
                raise  # unknown, not absent: stay resumable
            except GitHubError as exc:
                return self._reject_replan(txn, f"cannot list replacement PR candidates: {exc}")
            closed = find_non_open_claimant(all_prs, txn)
            if closed.disposition is not Disposition.NONE:
                return self._reject_replan(txn, closed.reason)
            if claimed_url:
                return self._reject_replan(
                    txn,
                    f"the agent reports replacement PR {claimed_url}, but no open PR in "
                    f"{state.repository} carries the marker for replan transaction "
                    f"{txn.transaction_id}",
                    claimed_url,
                )
            return None
        if selection.disposition is not Disposition.OK:
            return self._reject_replan(txn, selection.reason)
        pr, attestation = selection.pr, selection.attestation
        assert pr is not None and attestation is not None  # Disposition.OK invariant
        canonical = parse_pr_url(pr.url).canonical
        if claimed_url and not same_pr_url(claimed_url, canonical):
            return self._reject_replan(
                txn,
                f"the agent reports replacement PR {claimed_url}, but the PR bound to replan "
                f"transaction {txn.transaction_id} is {canonical}",
                claimed_url,
            )
        drift = (
            verify_target_pr(pr, txn, state.repository, require_checkpoint_head=False)
            or verify_attestation(attestation, txn)
            # The same snapshot that bound the candidate answers whether it
            # is the issue's one open implementation claimant, the source
            # aside: one of two is a PR the next ANALYZE_EXECUTE entry would
            # refuse to choose between, so the replan does not choose either.
            or verify_sole_implementation_claimant(open_prs, canonical, txn, state.repository)
        )
        if drift:
            return self._reject_replan(txn, drift, canonical)
        txn.replacement_pr_url = canonical
        txn.replacement_branch = pr.head_ref
        txn.replacement_head_sha = pr.head_sha
        txn.attested_findings_considered = attestation.findings_considered
        txn.attested_unique_constraints = attestation.unique_constraints
        txn.stage = ReplanStage.VERIFIED
        # Durable checkpoint before the controller's destructive write: from
        # here a crash resumes into disposition, never into a second agent run.
        self._save_replan_txn(txn)
        return None

    def _target_drift(self, target: PRInfo, open_prs: list[PRInfo], txn: ReplanTransaction) -> str:
        """Why the checkpointed replacement can no longer be acted on, or ``""``.

        The one rule for every read after binding: the final read before the
        close, the confirmation after it, and the activation. Objective facts
        at the checkpointed HEAD (:func:`verify_target_pr`), the transaction
        marker that proves provenance (:func:`verify_target_marker`), and the
        strict open listing that proves the replacement is the issue's one
        implementation claimant, the source aside
        (:func:`verify_sole_implementation_claimant`). Binding applies the
        same three rules on its own snapshot, with the HEAD becoming the
        checkpoint rather than being compared to it.
        """
        repository = self._require_state().repository
        return (
            verify_target_pr(target, txn, repository, require_checkpoint_head=True)
            or verify_target_marker(target, txn)
            or verify_sole_implementation_claimant(
                open_prs, txn.replacement_pr_url, txn, repository
            )
        )

    def _supersede_source(self, txn: ReplanTransaction) -> StepOutcome:
        """Close the source PR under a compensated two-sided swap, then activate.

        Closing is irreversible for the review evidence the source carries, so
        it happens only while *both* checkpoints still hold: the source is
        exactly the implementation the controller decided to replace, and the
        target is exactly the replacement it verified. Either side having
        drifted means the decision rests on facts that no longer exist.

        GitHub has no conditional close, so the compare cannot be fused to the
        write: a push, a close, or a body edit landing between the last read
        and ``gh pr close`` would otherwise stand. The comparison is therefore
        completed *after* the write, in :meth:`_confirm_supersede`, and a
        checkpoint that moved inside that window is compensated -- the source
        is reopened under a durable ``COMPENSATING`` record and the phase
        blocks -- rather than accepted. The pre-close checks are kept: they
        make the common refusal cost nothing.

        The comparison is completed on reads :meth:`_confirm_supersede` takes
        itself, after the receipt is published: the source snapshot this method
        reads back is only used to prove the close landed and to decide whether
        to publish the receipt at all.

        ``SUPERSEDE_INTENT`` is persisted before ``close_pr`` is called, so a
        crash anywhere in the write window resumes into disposition rather than
        into a second attempt. It records an *intent*, though, not a performed
        write, so it cannot by itself tell the controller's close from a human's
        in that window. Ownership is proven by a close receipt posted
        *after* the close is observed (:meth:`comment_pr`, checked by
        :meth:`_close_not_ours`): ``gh pr close --comment`` posts its comment
        before the close lands, so a receipt in it can predate the close and
        must never count as proof. A CLOSED source without the receipt is
        refused, and so is an OPEN one that carries it: a prior close landed
        and a human reopened the PR.

        An OPEN source *without* the receipt is where the receipt runs out:
        "the close never ran" and "it landed, lost its receipt to a crash, and
        a human reopened the PR" leave the same comments, and only the second
        is a decision a retry would override (R11-F1). The intent therefore
        also records the source's ``closed`` issue-event count, read just
        before it is persisted. Issue events are append-only and undeletable,
        so on resume the same count read again is proof either way
        (:func:`verify_close_never_ran`, #69): risen, and a close landed after
        the intent, so the reopen stands and nothing is closed; unchanged, and
        no close of any kind happened, so the write is retried -- once, through
        the same pre-close revalidation, with the attempt persisted before it
        -- and the receipt is posted by whichever attempt observes its own
        close. Absence of a comment never decides the write; the event count
        does.
        """
        state = self._require_state()
        unbound = self._replan_unbound(txn)
        if unbound is not None:
            return unbound
        if txn.stage is ReplanStage.SUPERSEDED:
            return self._activate_if_verified(txn)
        if txn.stage not in (ReplanStage.VERIFIED, ReplanStage.SUPERSEDE_INTENT):
            return self._reject_replan(
                txn, f"replan transaction reached the supersede step at stage {txn.stage.value!r}"
            )
        try:
            source = self.github.get_pr(txn.source_pr_url)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._reject_replan(txn, f"cannot read source PR {txn.source_pr_url}: {exc}")
        if txn.stage is ReplanStage.SUPERSEDE_INTENT and not source.is_open:
            # Intent recorded before the write is what makes this close ours.
            # A resume lands here having missed the close window entirely, so
            # it owes the same post-close comparison the writing step does.
            # It re-reads the source for itself; this snapshot is already one
            # round trip old by the time the comparison runs.
            return self._confirm_supersede(txn)
        if txn.stage is ReplanStage.SUPERSEDE_INTENT:
            # An OPEN source under a durable intent. The intent proves a close
            # was *about* to be attempted, not whether it was: "crashed before
            # `gh pr close` ran" and "closed, crashed before the receipt was
            # published, then reopened by a human" leave the same OPEN source
            # with no receipt, and only the second is a decision a retry
            # would override (R11-F1). The receipt can make the second story
            # certain; its absence never makes the first one so. What does is
            # the source's `closed` issue-event count against the one the
            # intent recorded (#69): unchanged proves no close happened since
            # the intent and the write below is retried once; anything else
            # refuses, naming the story the evidence supports.
            try:
                prior_comments = self.github.get_pr_comments(txn.source_pr_url)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return self._reject_replan(
                    txn,
                    f"source PR {txn.source_pr_url} is open under a recorded close intent, but "
                    f"its comments could not be read ({exc}), so a prior close cannot be ruled "
                    "out and the controller will not close it",
                )
            if has_close_receipt((c.body for c in prior_comments), txn.transaction_id):
                return self._reject_replan(
                    txn,
                    f"source PR {txn.source_pr_url} is open but already carries the close receipt "
                    f"for replan transaction {txn.transaction_id}; a prior close landed and was "
                    "then reopened, so the controller will not close it again",
                )
            try:
                closed_events = self.github.get_pr_close_event_count(txn.source_pr_url)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return self._reject_replan(
                    txn,
                    f"source PR {txn.source_pr_url} is open under a recorded close intent, but "
                    f"its issue events could not be read ({exc}), so a prior close cannot be "
                    "ruled out and the controller will not close it",
                )
            refusal = verify_close_never_ran(closed_events, txn)
            if refusal:
                return self._reject_replan(txn, refusal)
            # No close has happened since the intent: the write below runs
            # again, as the retry the journal will record before it.
        # The destructive write is ahead: either no attempt at it was ever
        # recorded (VERIFIED), or GitHub just proved the recorded one never
        # landed (SUPERSEDE_INTENT). Both revalidate both sides now.
        try:
            target = self.github.get_pr(txn.replacement_pr_url)
            open_prs = self.github.list_open_prs(state.repository)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"replacement PR {txn.replacement_pr_url} could not be re-read: {exc}",
                txn.replacement_pr_url,
            )
        drift = self._target_drift(target, open_prs, txn)
        if drift:
            return self._reject_replan(txn, drift, txn.replacement_pr_url)
        merge_base, unread = self._source_merge_base(source)
        if unread:
            return self._reject_replan(
                txn,
                f"the merge base of source PR {txn.source_pr_url} could not be read "
                f"({unread}), so the checkpoint cannot be confirmed before the close",
            )
        drift = verify_source_checkpoint(source, txn, merge_base)
        if drift:
            return self._reject_replan(txn, drift)
        if txn.stage is ReplanStage.VERIFIED:
            # The watermark a resume compares against. Read after the source
            # checkpoint held and immediately before the intent is persisted:
            # a count that lags GitHub can only be lower than the truth, and a
            # lower watermark only makes the resume refuse where it could
            # have retried, never the reverse.
            try:
                closed_events = self.github.get_pr_close_event_count(txn.source_pr_url)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return self._reject_replan(
                    txn,
                    f"the issue events of source PR {txn.source_pr_url} could not be read "
                    f"({exc}), so the close intent cannot record the closed-event count a "
                    "resume would need, and the controller will not close it",
                )
            txn.stage = ReplanStage.SUPERSEDE_INTENT
            txn.close_intent_at = utcnow_iso()
            txn.source_closed_event_count = closed_events
        txn.close_attempts += 1
        self._save_replan_txn(txn)
        try:
            # No receipt here: `gh pr close --comment` posts before the close
            # lands, so a receipt in it could predate the close (R8-F1).
            self.github.close_pr(
                txn.source_pr_url,
                f"Superseded by {txn.replacement_pr_url} after controller-detected review/fix "
                "non-convergence. This PR was closed without merge; the replacement starts from "
                f"the {txn.base_branch} branch. Replan transaction {txn.transaction_id}.",
            )
        except GitHubUnavailableError:
            # The close may or may not have landed. SUPERSEDE_INTENT is already
            # persisted, so `resume` reads GitHub and resolves it either way.
            raise
        except GitHubError as exc:
            # Our close did not land (it failed while the source was still
            # open, or lost a race a human already won). Adopting the CLOSED
            # source now would be adopting someone else's close.
            return self._reject_replan(txn, f"closing source PR {txn.source_pr_url} failed: {exc}")
        try:
            source = self.github.get_pr(txn.source_pr_url)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"source PR {txn.source_pr_url} could not be re-read after the close attempt "
                f"({exc}); the close may or may not have landed",
            )
        if source.state != "CLOSED":
            return self._reject_replan(
                txn,
                f"source PR {txn.source_pr_url} is {source.state or '(unknown)'} after the close "
                "attempt; expected CLOSED",
            )
        # The close landed under this transaction: publish the ownership
        # receipt now, so a resume can tell this close from a human's. Posted
        # only by the attempt that observed its own close.
        try:
            self.github.comment_pr(txn.source_pr_url, render_close_receipt(txn.transaction_id))
        except GitHubUnavailableError:
            raise  # receipt unknown: resume sees CLOSED without one and blocks
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"source PR {txn.source_pr_url} was closed, but the close receipt could not be "
                f"published ({exc}), so the close cannot be proven on resume",
            )
        return self._confirm_supersede(txn)

    def _confirm_supersede(self, txn: ReplanTransaction) -> StepOutcome:
        """The swap half of the compare-and-swap, completed after the close.

        Reached with the source CLOSED and the intent durable -- either
        straight from the write, or on a ``resume`` that missed it. Ownership
        is established first, then both checkpoints are compared once more
        against GitHub; if either moved, the close landed on facts that had
        already changed and is *undone* (:meth:`_compensate_close`) instead of
        being activated. Only an unmoved pair reaches ``SUPERSEDED``.

        Both sides are re-read *here*, by this method, rather than reused from
        whatever the caller last saw (R9-F1). The writing step's post-close
        read happens before the receipt is published, and the resume path's
        happens before the comments are fetched, so either snapshot is already
        a round trip stale by the time it would be compared -- and a source
        that moved inside that gap has to reach the compensation path, not be
        waved through to be caught later by :meth:`_activate_if_verified`,
        which blocks without undoing the close.

        The window cannot be *removed*, only moved: ``gh pr close`` takes no
        precondition and ``SUPERSEDED`` is persisted after the last read, so
        the boundary is exactly "the last read before the write". Drift before
        it is compensated; drift after it is terminal (see
        :meth:`_activate_if_verified`), because by then the close had already
        been confirmed correct. Making that boundary as late as possible is
        the whole of what an implementation can do.

        An unconfirmable checkpoint counts as drift, not as absence of it: a
        conclusive read failure on either side means the close cannot be shown
        to have been correct, and an unproven close is undone rather than kept.
        A *transient* GitHub failure propagates untouched: the transaction
        stays at ``SUPERSEDE_INTENT``, and a resume runs exactly this method
        again on the same source PR.
        """
        state = self._require_state()
        unowned = self._close_not_ours(txn)
        if unowned:
            return self._reject_replan(txn, unowned)
        try:
            source = self.github.get_pr(txn.source_pr_url)
            merge_base, unread = self._source_merge_base(source)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            drift = (
                f"source PR {txn.source_pr_url} could not be re-read after the close "
                f"({exc}), so the close cannot be confirmed against its checkpoint"
            )
        else:
            drift = (
                f"the merge base of source PR {txn.source_pr_url} could not be re-read after "
                f"the close ({unread}), so the close cannot be confirmed against its checkpoint"
                if unread
                else verify_closed_source(source, txn, merge_base)
            )
        if not drift:
            try:
                target = self.github.get_pr(txn.replacement_pr_url)
                open_prs = self.github.list_open_prs(state.repository)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                drift = (
                    f"replacement PR {txn.replacement_pr_url} could not be re-read after the "
                    f"close ({exc}), so the replacement cannot be confirmed"
                )
            else:
                drift = self._target_drift(target, open_prs, txn)
        if not drift:
            return self._record_supersede(txn)
        return self._compensate_close(txn, drift)

    def _close_not_ours(self, txn: ReplanTransaction) -> str:
        """Why this transaction may not claim the source's close, or ``""``.

        ``SUPERSEDE_INTENT`` is written before ``gh pr close`` and therefore
        records only an *intended* write. A crash in that window, followed by
        a human closing the source, would otherwise be indistinguishable from
        the controller's own close -- and the resume would supersede on the
        strength of somebody else's action. The receipt
        (:func:`render_close_receipt`) is therefore posted with
        :meth:`comment_pr` *after* the close is observed, never inside the
        ``gh pr close --comment`` that predates it: presence proves this
        transaction closed the PR; absence proves only that the close cannot
        be attributed to it -- a human may have closed it, or this transaction
        may have closed it and crashed before the receipt was published. A
        resume never posts a receipt.

        Absence is conclusive: the run refuses and blocks, leaving the close
        exactly as the human made it -- the controller must not reopen a PR it
        did not close. A transient read failure is not absence and propagates.
        """
        try:
            comments = self.github.get_pr_comments(txn.source_pr_url)
        except GitHubUnavailableError:
            raise  # unknown, not unowned: `resume` re-reads
        except GitHubError as exc:
            return (
                f"source PR {txn.source_pr_url} is closed, but its comments could not be read "
                f"({exc}), so the close cannot be attributed to replan transaction "
                f"{txn.transaction_id}"
            )
        if has_close_receipt((c.body for c in comments), txn.transaction_id):
            return ""
        return (
            f"source PR {txn.source_pr_url} is closed but carries no close receipt for replan "
            f"transaction {txn.transaction_id}, so the close cannot be attributed to this "
            "transaction (a human may have closed it, or this transaction may have closed it "
            "and crashed before the receipt was published); refusing to supersede on a close "
            "the controller cannot prove it performed"
        )

    def _compensate_close(self, txn: ReplanTransaction, drift: str) -> StepOutcome:
        """Decide, durably, to undo the close -- then carry the decision out.

        The undo is the reason the phase may claim compare-and-swap semantics
        over an API that cannot express them: the close is not permitted to
        stand on facts that had already moved. Recording ``COMPENSATING``
        *before* the reopen is what makes the undo itself crash-safe. A reopen
        that succeeds and is then lost to a crash would otherwise leave the
        transaction at ``SUPERSEDE_INTENT`` over an OPEN source, and a resume
        whose drift had meanwhile settled back would close it a second time --
        laundering a refusal into a completed supersede, which is exactly what
        rejection monotonicity forbids.
        """
        txn.stage = ReplanStage.COMPENSATING
        txn.compensating_at = utcnow_iso()
        txn.compensation_reason = drift
        self._save_replan_txn(txn)
        return self._run_compensation(txn)

    def _run_compensation(self, txn: ReplanTransaction) -> StepOutcome:
        """Reopen the source the controller closed, and report what happened.

        Idempotent, and the single entry point for both the writing step and a
        ``resume`` at ``COMPENSATING``: a source already OPEN needs no reopen,
        only the confirmation. ``close_pr`` never deletes the branch, so the
        reopened PR carries its commits and review history intact. A failed or
        unconfirmed reopen is stated as such and names the manual step -- the
        controller never reports an undo it did not observe -- while a
        transient failure leaves the stage untouched so the resume replays the
        confirmation rather than closing twice.
        """
        drift = txn.compensation_reason or "the replan checkpoint no longer held at the close"
        comment = (
            f"AutoForge closed this pull request as superseded by {txn.replacement_pr_url}, "
            f"then found that the replan checkpoint no longer held at the moment of the close "
            f"({drift}). The close has been undone and the run has stopped for a human. "
            f"Replan transaction {txn.transaction_id}."
        )
        try:
            source = self.github.get_pr(txn.source_pr_url)
            if not source.is_open:
                self.github.reopen_pr(txn.source_pr_url, comment)
                source = self.github.get_pr(txn.source_pr_url)
        except GitHubUnavailableError:
            raise  # unresolved, not refused: the transaction stays resumable
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"{drift}; the source PR was closed inside the compare-and-swap window and "
                f"could not be reopened ({exc}), so it must be reopened by hand",
                txn.replacement_pr_url,
            )
        if not source.is_open:
            return self._reject_replan(
                txn,
                f"{drift}; the source PR is still {source.state or '(unknown)'} after the "
                "reopen attempt and must be reopened by hand",
                txn.replacement_pr_url,
            )
        return self._reject_replan(
            txn,
            f"{drift}; the close was undone and {txn.source_pr_url} is open again",
            txn.replacement_pr_url,
        )

    def _record_supersede(self, txn: ReplanTransaction) -> StepOutcome:
        txn.stage = ReplanStage.SUPERSEDED
        txn.superseded_at = utcnow_iso()
        self._save_replan_txn(txn)
        return self._activate_if_verified(txn)

    def _activate_if_verified(self, txn: ReplanTransaction) -> StepOutcome:
        """Re-derive both checkpoints from GitHub, then activate.

        ``SUPERSEDED`` is persisted before the replacement is installed into
        controller state, so there is a window in which the transaction says
        "activate this PR" while GitHub no longer agrees: the replacement can
        be closed, retargeted, moved, or have its marker edited before the
        activation is written. Activating from the journal alone would install
        a PR the controller can no longer prove anything about, so the same
        predicates that authorised the close are re-applied on the last read
        before the write -- by the writing step and by a ``resume`` at
        ``SUPERSEDED`` alike, so the two cannot decide differently.

        Drift here is terminal, not compensable: the close was confirmed
        correct when it happened -- against reads :meth:`_confirm_supersede`
        took after the receipt, the latest point at which anything could be
        checked -- and the source is legitimately closed. Undoing a close that
        was right when it was made would be its own kind of laundering, so the
        run blocks and says so, naming both PRs. Ownership of that close is
        already durable in ``SUPERSEDED`` and is not re-litigated.
        """
        state = self._require_state()
        try:
            source = self.github.get_pr(txn.source_pr_url)
            merge_base, unread = self._source_merge_base(source)
            target = self.github.get_pr(txn.replacement_pr_url)
            open_prs = self.github.list_open_prs(state.repository)
        except GitHubUnavailableError:
            raise  # unknown, not refused: `resume` re-reads and re-verifies
        except GitHubError as exc:
            unread = str(exc)
        if unread:
            # The merge base is read beside the PRs and a conclusive failure
            # of that read is the same refusal as one of theirs: a checkpoint
            # that cannot be re-derived is not confirmed.
            return self._reject_replan(
                txn,
                f"the source PR was closed, but the replan checkpoints could not be re-read "
                f"before activating the replacement ({unread})",
                txn.replacement_pr_url,
            )
        drift = verify_closed_source(source, txn, merge_base) or self._target_drift(
            target, open_prs, txn
        )
        if drift:
            return self._reject_replan(
                txn,
                f"the source PR was closed, but the replacement can no longer be activated: "
                f"{drift}",
                txn.replacement_pr_url,
            )
        return self._activate_replacement(txn)

    def _activate_replacement(self, txn: ReplanTransaction) -> StepOutcome:
        """Make the verified replacement the current implementation (idempotent).

        The supersede is recorded once per transaction id, and the replan
        budget (``escalation_count``) and ``execution_attempt`` move together
        with that record, never on their own: a replay of the same durable
        ``SUPERSEDED`` transaction installs the same replacement again but
        counts it once, as the merge counter counts a merged PR once. The run
        binding refuses such a replay before it reaches here (the journal's
        source is no longer the run's PR); this keeps the activation itself
        idempotent instead of relying on that.
        """
        state = self._require_state()
        already_counted = any(
            item.get("transaction_id") == txn.transaction_id for item in state.superseded_prs
        )
        if not already_counted:
            state.execution_attempt += 1
            state.escalation_count += 1
            state.superseded_prs.append(
                {
                    "pr_url": txn.source_pr_url,
                    "branch": txn.source_branch,
                    "head_sha": txn.source_head_sha,
                    "base_ref": txn.source_base_ref,
                    "merge_base_sha": txn.source_merge_base_sha,
                    "review_round": txn.source_review_round,
                    "replacement_pr_url": txn.replacement_pr_url,
                    "reason": txn.escalation.get("trigger", "replan"),
                    "transaction_id": txn.transaction_id,
                    "historical_findings_considered": txn.attested_findings_considered,
                    "unique_failure_constraints": txn.attested_unique_constraints,
                    "superseded_at": txn.superseded_at or utcnow_iso(),
                }
            )
        state.current_pr_url = txn.replacement_pr_url
        state.current_branch = txn.replacement_branch
        state.current_head_sha = txn.replacement_head_sha
        # The activation predicate verified the replacement's base is the
        # branch the transaction was based on (replan_txn.verify_activation).
        state.current_base_ref = txn.base_branch
        state.current_merge_base_sha = ""  # bound at REVIEW entry
        state.reviewed_pr_url = ""
        state.reviewed_head_sha = ""
        state.reviewed_base_ref = ""
        state.reviewed_merge_base_sha = ""
        # review_round counts completed rounds; 0 means the next replacement
        # review is the required fresh round 1.
        state.review_round = 0
        state.review_history = []
        state.open_findings = []
        state.prior_findings = []
        state.last_fix_resolutions = []
        state.last_review_comment_url = ""
        state.last_review_result = ""
        state.last_review_needs_fix = None
        state.replan_transaction = {}
        nxt = self._next_phase(Phase.REPLAN_REEXECUTE, {})
        validate_transition(Phase.REPLAN_REEXECUTE, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.REPLAN_REEXECUTE,
            message=(
                f"replacement PR {txn.replacement_pr_url} verified (replan transaction "
                f"{txn.transaction_id}); source PR {txn.source_pr_url} closed without merge; "
                f"REPLAN_REEXECUTE -> {nxt.value} (fresh round 1)"
            ),
        )

    # -- agent invocation + correction retry ---------------------------------------
    def _log_metadata(self, phase: Phase) -> dict[str, object]:
        state = self._require_state()
        if state.mode == WorkflowMode.LOCAL:
            return {
                "mode": "LOCAL",
                "feature_spec_path": state.feature_spec_path,
                "feature_spec_sha256": state.feature_spec_sha256,
                "workspace_fingerprint": state.workspace_fingerprint,
            }
        if phase == Phase.REPLAN_REEXECUTE:
            return self._replan_log_metadata()
        return {}

    @overload
    def _invoke_phase(self, phase: Phase) -> dict: ...

    @overload
    def _invoke_phase(
        self, phase: Phase, reconcile: Callable[[], StepOutcome | None]
    ) -> dict | StepOutcome: ...

    def _invoke_phase(
        self, phase: Phase, reconcile: Callable[[], StepOutcome | None] | None = None
    ) -> dict | StepOutcome:
        """Launch the phase's agent, correcting a malformed result within bound.

        ``reconcile`` is the phase-entry reconciliation (:meth:`_remote_entry`)
        and is called before every correction relaunch: the agent that
        returned the malformed result may already have done the phase's
        GitHub work, and the relaunch must see it exactly as ``resume``
        would. An outcome from it resolves the phase without relaunching and
        is returned in place of a payload. A LOCAL run passes none: its
        write phases are judged against the durable launch checkpoint and
        its review verification refuses a tree the reviewer changed.
        """
        state = self._require_state()
        profile = profile_for_phase(self.config, phase, state.review_round)
        provider = self.providers.get(profile)
        provider.validate_profile(profile)
        limits = self.config.agent_limits(profile)
        max_corrections = max(0, self.config.execution.max_correction_attempts)
        # Read once per invocation, before the loop: the agent and the record
        # of it are launched from the same directory, and for a LOCAL run
        # that directory comes from the contract (see :meth:`_execution_cwd`).
        cwd = self._execution_cwd()
        # Names only, never values: what the agent may inherit from the
        # controller's environment (the provider adds its own CLI's names).
        env_allowlist = self.config.execution.environment_names()
        # The logger is opened -- and the run directory proved to take a
        # new entry, the event journal proved appendable -- *before* the
        # agent is launched, not at the first write after it returns. The
        # run log lives where the agents write, so opening it can refuse (a
        # run directory the controller cannot publish into, a journal that
        # is a link or a FIFO or past
        # :data:`autoforge.runlog.MAX_EVENT_JOURNAL_BYTES`); a refusal must
        # land before a write-capable agent has done work that would then go
        # unlogged. The journal is never read: the step sequence comes from
        # the run's step directories. One logger serves every attempt of this
        # invocation; each write still re-verifies the state-directory
        # capability it goes through.
        logger = self._logger()
        correction_error: str | None = None
        attempt = 0
        while True:
            if correction_error is not None and reconcile is not None:
                # The malformed attempt is a launch that returned; whatever it
                # wrote to GitHub is reconciled before anything is launched
                # again, and the prompt below is rendered from that.
                resolved = reconcile()
                if resolved is not None:
                    return resolved
            # Which UPDATE_EPIC result this launch asks for (ADR 0004 D4.7):
            # decided by the entry above, and the schema its result is held to.
            request = (
                self._update_epic_request()
                if phase == Phase.UPDATE_EPIC
                else UpdateEpicRequest.FULL
            )
            prompt = self.render_prompt_for(phase, correction_error)
            # The launch is charged to the durable LOCAL bound here, after
            # every step that can refuse without launching and immediately
            # before the one that launches. The first launch of an entry is
            # never refused (``_local_step_once`` already blocked a spent
            # entry); a correction can be, and is then not re-invoked.
            refusal = self._charge_local_launch(phase)
            if refusal:
                raise ControlResultValidationError(
                    f"agent '{profile.name}' did not return a valid CONTROL_RESULT after "
                    f"{attempt} attempt(s): {correction_error}. Not re-invoked: {refusal}"
                )
            attempt += 1
            state.attempt += 1
            if state.mode == WorkflowMode.REMOTE:
                # D13.3: which publication contract this launch runs under,
                # so a later version can tell a launch of its own from one
                # whose agent published under the previous contract.
                state.launch_label = launch_label_for(phase)
            # The one write before the agent starts, in both modes: the
            # attempt counter and, for a LOCAL write phase, the checkpoint
            # charged just above land together, and for a REMOTE phase the
            # entry observation (D4.4) and the launch label. A crash while the
            # agent runs, or a refused run-log write after it returned,
            # therefore never leaves controller state claiming the phase was
            # not yet attempted (#55, PR #89).
            self._save()
            # The step's log directory and its ``progress.log`` are published
            # here, after the attempt is persisted and before the launch, so
            # the progress lines can be followed while the agent runs and
            # survive a kill. A refusal lands like a crash between the save
            # and the launch: nothing was launched.
            step_log = self._begin_step(logger, phase)
            req = AgentRequest(
                phase=phase.value,
                prompt=prompt,
                cwd=cwd,
                profile=profile,
                idle_timeout_seconds=limits.idle_timeout_seconds,
                max_runtime_seconds=limits.max_runtime_seconds,
                attempt=state.attempt,
                correction=correction_error is not None,
                env_allowlist=env_allowlist,
                loop_detection=self.config.execution.loop_detection,
            )
            record = ExecutionRecord(
                run_id=state.run_id,
                seq=0,
                phase=phase.value,
                attempt=state.attempt,
                correction=correction_error is not None,
                issue_url=state.current_issue_url,
                pr_url=state.current_pr_url,
                review_round=state.review_round,
                profile=profile.name,
                provider=profile.provider,
                model=profile.model,
                effort=profile.effort,
                prompt_version=state.prompt_version,
                command=provider.build_command_for(profile, prompt),
                cwd=cwd,
                idle_timeout_seconds=limits.idle_timeout_seconds,
                max_runtime_seconds=limits.max_runtime_seconds,
                loop_detection=self.config.execution.loop_detection.as_dict(),
                metadata={
                    **self._log_metadata(phase),
                    "env_allowlist": list(provider.environment_allowlist(req) or ()),
                },
            )
            result: AgentExecutionResult | None = None
            try:
                result = self._launch(provider, req, limits, step_log, phase)
            except ExecutionError as exc:
                record.error = f"{type(exc).__name__}: {exc}"
                self._record_invocation(logger, record, prompt, "", "", phase, step_log)
                raise
            record.started_at = result.started_at
            record.finished_at = result.finished_at
            record.exit_code = result.exit_code
            record.timed_out = result.timed_out
            record.timeout_limit = result.timeout_limit
            record.last_activity_at = result.last_activity_at
            record.ended_by = (result.timeout_limit or "timeout") if result.timed_out else "exit"
            if result.loop is not None:
                record.loop = result.loop.record()
                record.loop_warnings = result.loop.warnings
            record.stdout_truncated = result.stdout_truncated
            record.stderr_truncated = result.stderr_truncated
            # What the invocation left behind is recorded whatever its
            # outcome, so the log names a leftover process (a server the
            # agent started, a member the kill could not remove) rather
            # than presenting a slow agent or a clean kill (#85).
            record.descendants_killed = result.descendants_killed
            record.group_survived_kill = result.group_survived_kill
            record.capture_abandoned = result.capture_abandoned
            record.orphans_killed = result.orphans_killed
            record.orphan_survived_kill = result.orphan_survived_kill
            record.orphans_unchecked = result.orphans_unchecked
            # The adapter's own summary of the run (bounded, redacted, flat);
            # recorded as given and never interpreted here.
            record.provider_summary = dict(result.provider_summary)
            stdout, stderr = result.stdout or "", result.stderr or ""
            if result.timed_out:
                # A loop kill (#194) is a timeout in every respect but its text.
                loop = _loop_killed(result)
                reached = f"killed: {loop}" if loop else _limit_reached(limits, result)
                killed = f"was killed: {loop}" if loop else f"{reached} and was killed"
                record.error = _with_leftovers(reached, result.leftovers)
                self._record_invocation(logger, record, prompt, stdout, stderr, phase, step_log)
                raise ExecutionTimeoutError(
                    _with_leftovers(f"agent '{profile.name}' {killed}", result.leftovers)
                    + ". State unchanged — inspect the real Git/GitHub state, then 'resume'."
                )
            if result.provider_failure:
                # The run failed inside the provider's protocol, whatever the
                # exit status: handled exactly like a failed exit, and checked
                # before it because the reason says more (ADR 0003 §2.6).
                record.error = _with_leftovers(result.provider_failure, result.leftovers)
                self._record_invocation(logger, record, prompt, stdout, stderr, phase, step_log)
                raise ExecutionError(
                    _with_leftovers(
                        f"agent '{profile.name}' failed: {result.provider_failure}",
                        result.leftovers,
                    )
                    + f". stderr tail: {stderr[-2000:]} "
                    "State unchanged — inspect logs, then 'resume'."
                )
            if result.exit_code != 0:
                record.error = _with_leftovers(f"exit {result.exit_code}", result.leftovers)
                self._record_invocation(logger, record, prompt, stdout, stderr, phase, step_log)
                raise ExecutionError(
                    _with_leftovers(
                        f"agent '{profile.name}' exited {result.exit_code}", result.leftovers
                    )
                    + f". stderr tail: {stderr[-2000:]} "
                    "State unchanged — inspect logs, then 'resume'."
                )
            try:
                # Only the tail of a truncated stdout is searched: the block
                # is the last thing the agent writes, so the kept tail holds
                # a whole one; a block before the cut is stale or spans it.
                payload = parse_control_result(
                    result.stdout_tail, phase, state.mode, update_epic_request=request
                )
                if (
                    phase == Phase.UPDATE_EPIC
                    and state.mode == WorkflowMode.REMOTE
                    and payload.get("status") == "success"
                ):
                    # The controller's half of the schema (a required roadmap
                    # section, the publishable comment as a whole): refused
                    # here, it is corrected before anything is posted.
                    self._check_update_epic_result(payload, request)
                if (
                    phase == Phase.ANALYZE_EXECUTE
                    and state.mode == WorkflowMode.REMOTE
                    and payload.get("status") == "success"
                ):
                    # The candidate the controller would publish, read from
                    # the worktree itself: refused here, it is corrected
                    # before anything is pushed or created (#161).
                    self._check_analyze_result(payload, cwd)
                if (
                    phase == Phase.REVIEW
                    and state.mode == WorkflowMode.REMOTE
                    and payload.get("status") == "success"
                ):
                    # The comment the controller would post, rendered from the
                    # result: refused here, it is corrected before anything is
                    # posted (#162).
                    self._check_review_result(payload)
                if (
                    phase == Phase.FIX
                    and state.mode == WorkflowMode.REMOTE
                    and payload.get("status") == "success"
                ):
                    # The resolutions and the candidate the controller would
                    # publish, read from the worktree itself: refused here,
                    # it is corrected before anything is created or pushed
                    # (#163).
                    self._check_fix_result(payload, cwd)
            except VerificationError as exc:
                # A read of the candidate that could not be completed: not the
                # agent's error, so no correction; 'resume' reads it again.
                record.error = f"{type(exc).__name__}: {exc}"
                self._record_invocation(logger, record, prompt, stdout, stderr, phase, step_log)
                raise
            except (ControlResultError, ControlResultValidationError) as exc:
                detail = str(exc)
                if result.stdout_truncated:
                    detail += (
                        f" (stdout exceeded the {DEFAULT_MAX_OUTPUT_BYTES}-byte capture bound; "
                        "only the last part of it was searched -- keep the transcript short "
                        "and end it with the CONTROL_RESULT block)"
                    )
                record.error = f"{type(exc).__name__}: {detail}"
                self._record_invocation(logger, record, prompt, stdout, stderr, phase, step_log)
                if attempt <= max_corrections:
                    # A correction re-launches the same write-capable agent;
                    # the top of the loop charges it against the same durable
                    # bound as the launch that preceded it, or refuses.
                    correction_error = f"{type(exc).__name__}: {detail}"
                    continue
                raise ControlResultValidationError(
                    f"agent '{profile.name}' did not return a valid CONTROL_RESULT after "
                    f"{attempt} attempt(s): {detail}"
                ) from exc
            record.parsed_result = payload
            self._record_invocation(logger, record, prompt, stdout, stderr, phase, step_log)
            return payload

    def _begin_step(self, logger: RunLogger, phase: Phase) -> StepLog:
        """Publish the launch's step directory and open its ``progress.log``, or refuse."""
        state = self._require_state()
        try:
            return logger.begin_step(phase.value, state.attempt)
        except StateError as exc:
            # Re-raised as the same object, as in :meth:`_record_invocation`;
            # the attempt was persisted, so this is a crash before the launch.
            exc.args = (
                f"{exc} -- the run log refused the step directory of {phase.value} before "
                "the agent was launched, so nothing was launched; repair the log directory, "
                "then 'resume'",
            )
            raise

    def _progress_label(self, phase: Phase) -> str:
        """The prefix of a launch's progress lines: the phase, and the issue in REMOTE mode."""
        state = self._require_state()
        if state.mode == WorkflowMode.REMOTE and state.current_issue_url:
            try:
                return f"{phase.value} #{parse_issue_url(state.current_issue_url).number}"
            except ConfigurationError:
                pass
        return phase.value

    def _launch(
        self,
        provider,
        req: AgentRequest,
        limits: AgentLimits,
        step_log: StepLog,
        phase: Phase,
    ) -> AgentExecutionResult:
        """Run the agent with its live progress reported, then close the progress log.

        The lines go to the step's ``progress.log`` and to
        :attr:`progress_output` when set. One line before the launch names
        what is launched, where, and where its log is; one after says how it
        ended. The reporter's heartbeat thread is joined before this
        returns, and progress never changes the outcome: an output that
        fails is dropped (:class:`autoforge.progress.ProgressReporter`).
        """
        profile = req.profile
        outputs: list[Callable[[str], None]] = [step_log.append]
        if self.progress_output is not None:
            outputs.append(self.progress_output)
        try:
            with ProgressReporter(self._progress_label(phase), outputs) as reporter:
                reporter.line(
                    f"launching {profile.name} ({profile.provider}, model "
                    f"{profile.model or 'default'}, effort {profile.effort or 'default'}), "
                    f"{limits.describe()}, loop detection {req.loop_detection.mode}, "
                    f"attempt {req.attempt}, "
                    f"worktree {req.cwd}, log {step_log.path}"
                )
                req.progress = reporter.sink
                try:
                    result = provider.execute(req)
                except ExecutionError as exc:
                    reporter.line(f"agent could not be run: {type(exc).__name__}")
                    raise
                loop = _loop_killed(result)
                if loop:
                    end = f"killed: {loop}"
                elif result.timed_out:
                    end = f"{_limit_reached(limits, result)}, killed"
                elif result.provider_failure:
                    end = f"failed: {result.provider_failure}"
                else:
                    end = f"exited {result.exit_code}"
                reporter.line(f"agent {end}, {reporter.events} progress events")
                return result
        finally:
            step_log.close()

    def _record_invocation(
        self,
        logger: RunLogger,
        record: ExecutionRecord,
        prompt: str,
        stdout: str,
        stderr: str,
        phase: Phase,
        step_log: StepLog | None = None,
    ) -> None:
        """Publish the invocation's artifacts and journal line, or refuse loudly.

        The one exit for every outcome of a launch (provider error, timeout,
        non-zero exit, malformed result, accepted result). The launch itself
        was persisted before the agent started, so a refusal here (a journal
        an agent enlarged past its budget, #55; a run directory it filled; an
        I/O failure) loses no controller state; what it does mean is that the
        run log can no longer record what agents do, so the phase is left
        unchanged and nothing is launched again -- not even a correction --
        until the operator repairs it. The refusal names the invocation's own
        outcome, so the failure that was being recorded is not masked by the
        failure to record it, and says what ``resume`` will do, which differs
        by mode: a LOCAL launch was checkpointed and is retried against that
        checkpoint; a REMOTE re-entry first reconciles with GitHub, where the
        agent's side effects may already be.
        """
        state = self._require_state()
        try:
            logger.log_execution(record, prompt, stdout, stderr, step_log=step_log)
        except StateError as exc:
            # Re-raised as the same object: its type is the filesystem cause
            # (an oversized journal, an unsafe path) and callers distinguish
            # on it. Only the message grows.
            outcome = record.error or "a CONTROL_RESULT the controller accepted"
            if state.mode == WorkflowMode.LOCAL:
                follow_up = (
                    "Its launch was checkpointed before it started, so once the log "
                    f"directory is repaired 'resume' re-enters {phase.value} as a retry "
                    "judged against that checkpoint"
                )
            else:
                reentry = _REMOTE_REENTRY_RECONCILIATION[phase]
                follow_up = (
                    "Its GitHub side effects (a comment, a push, a PR) may exist while the "
                    "controller state does not record them. Repair the log directory, then "
                    f"'resume': it re-enters {phase.value} and {reentry}"
                )
            exc.args = (
                f"{exc}. This refusal interrupted attempt {record.attempt} of {phase.value} "
                f"after the agent had returned with: {outcome}. {follow_up}.",
            )
            raise

    # -- verification + state application -------------------------------------------
    def _verify_and_apply(self, phase: Phase, payload: dict) -> tuple[Phase, str]:
        if phase == Phase.ANALYZE_EXECUTE:
            return self._apply_analyze(AnalyzeExecuteResult.from_payload(payload))
        if phase == Phase.REVIEW:
            return self._apply_review(ReviewResult.from_payload(payload))
        if phase == Phase.FIX:
            return self._apply_fix(FixResult.from_payload(payload))
        if phase == Phase.REPLAN_REEXECUTE:
            return self._apply_replan(ReplanReexecuteResult.from_payload(payload))
        if phase == Phase.UPDATE_EPIC:
            return self._apply_update_epic(payload)
        raise StateTransitionError(f"phase {phase.value} does not accept agent results")

    def _apply_analyze(self, res: AnalyzeExecuteResult) -> tuple[Phase, str]:
        """Plan, publish and complete ANALYZE_EXECUTE from the accepted candidate (#161).

        The result was checked before it was accepted
        (:meth:`_check_analyze_result`): the candidate is the worktree's HEAD
        as the controller read it. A precondition read then finds GitHub as
        the entry observed it: the default branch is the one it read, no
        open PR carries the issue's marker, the branch is at the head the
        entry recorded and the open PR on it, if any, is the one the entry
        recorded; anything else is unexplained and blocks with nothing
        planned or sent (ADR 0004 D9.4, K2, K3). The push (K1) and
        the PR (K2 to open it, K3 to adopt) are planned and saved with the
        completion context in one save, and :meth:`_complete_analyze` does
        the rest from what was saved, as a later entry would.
        """
        state = self._require_state()
        entry = self._analyze_entry
        if (
            entry is None
            or not same_issue_url(entry.issue_url, state.current_issue_url)
            or self._analyze_candidate != res.head_sha
        ):
            raise StateError(
                "an ANALYZE_EXECUTE result was applied without the candidate its check accepted"
            )
        branch = self.branch_name_for(state.current_issue_url)
        ref = f"refs/heads/{branch}"
        pr, problem = self._implementation_pr_candidate()
        if problem:
            return Phase.BLOCKED, problem
        if pr is not None:
            return Phase.BLOCKED, self._unjournaled_pr_text(parse_pr_url(pr.url).canonical)
        try:
            default_branch = self.github.get_repo(state.repository).default_branch
            if default_branch != entry.default_branch:
                return Phase.BLOCKED, (
                    self._default_branch_changed_text(entry.default_branch, default_branch)
                    + ". Nothing was planned, pushed or created. 'unblock' starts a fresh entry "
                    "that reads the default branch again"
                )
            head = self._branch_head(branch)
            adopt, problem = self._analyze_branch_pr(branch, entry.default_branch)
            adopt_body = self.github.get_pr(adopt.url).body if adopt is not None else ""
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"the default branch, branch {branch!r} or the PRs headed at it could not be "
                f"read before publishing: {exc}. This is not a transient GitHub failure, so "
                "nothing was pushed or created. Fix the cause, then 'unblock'"
            )
        if problem:
            return Phase.BLOCKED, problem
        if head != entry.remote_head:
            return Phase.BLOCKED, self._unjournaled_branch_text(branch, entry.remote_head, head)
        adopt_url = parse_pr_url(adopt.url).canonical if adopt is not None else ""
        if not _same_pr(adopt_url, entry.adopt_url):
            return Phase.BLOCKED, self._unobserved_branch_pr_text(
                branch, entry.adopt_url, adopt_url
            )
        owner = EffectOwner(
            run_id=state.run_id,
            phase=Phase.ANALYZE_EXECUTE,
            issue_url=state.current_issue_url,
            pr_url=state.current_pr_url,
            transaction_id="",
        )
        marker = render_implementation_marker(state.current_issue_url)
        push = EffectRecord.plan(
            0,
            EffectKind.PUSH,
            owner,
            identity={"repository": state.repository, "ref": ref, "candidate_sha": res.head_sha},
            target={"repository": state.repository, "ref": ref},
            precondition={"expected_old": entry.remote_head, "base_sha": entry.base_sha},
            payload={"sha": res.head_sha},
        )
        if adopt is None:
            second = EffectRecord.plan(
                1,
                EffectKind.IMPLEMENTATION_PR,
                owner,
                identity={"repository": state.repository, "marker": marker, "head_branch": branch},
                target={
                    "repository": state.repository,
                    "base": entry.default_branch,
                    "head": branch,
                },
                precondition={"absent": True},
                payload={"title": res.pr_title, "body": self._analyze_pr_body(res.pr_body)},
            )
        else:
            url = adopt_url
            block = self._analyze_closing_block()
            body = compose_append(adopt_body, block)
            problem = append_problem(f"PR {url}", IMPLEMENTATION, body)
            if problem:
                return Phase.BLOCKED, (
                    f"open PR {url} on {branch!r} cannot be adopted: {problem}. Nothing was "
                    "pushed or written. Fix its body, then 'unblock'"
                )
            second = EffectRecord.plan(
                1,
                EffectKind.ADOPT_PR,
                owner,
                identity={"pr_url": url, "marker": marker},
                target={"pr_url": url},
                precondition={"base_sha256": sha256_text(adopt_body)},
                payload={"body": body, "block": block},
            )
        state.effect_records = [push.to_dict(), second.to_dict()]
        state.completion_context = AnalyzeContext(state.current_issue_url).to_dict()
        # Strictly validated exactly as a later load will validate it, against
        # the entry observation the pre-launch save persisted: a plan that
        # fails is never written.
        state.phase_effects()
        self._save()
        return self._complete_analyze()

    def _complete_analyze(self) -> tuple[Phase, str]:
        """Finish ANALYZE_EXECUTE from the persisted plan: push, open or adopt, read back.

        The same code for the step that saved the plan and for every later
        entry (journal first): a PR create still pending blocks with nothing
        sent when its planned base is no longer the default branch; each
        record is reconciled against GitHub and issued at most once, in plan
        order, so the PR is never opened or adopted before the branch holds
        the candidate, and a PR to adopt is written to only once GitHub shows
        it at the candidate, onto the default branch. Then the PR is
        read back as the one open PR carrying the issue's marker, the one the
        record observed, headed at exactly the pushed HEAD on the
        controller's branch of this repository and based on the default
        branch, and bound. A conflict or a conclusive failure blocks with the
        records as persisted; an unavailable GitHub, a write whose outcome
        is not readable yet, or a PR head GitHub has not moved yet,
        propagates for 'resume'.
        """
        state = self._require_state()
        effects = state.phase_effects()
        if not isinstance(effects.context, AnalyzeContext) or len(effects.records) != 2:
            raise StateError("ANALYZE_EXECUTE has no persisted push and PR plan to complete")
        records = list(effects.records)
        push = records[0]
        branch = str(push.target["ref"]).removeprefix("refs/heads/")
        candidate = str(push.payload["sha"])
        expected_old = push.precondition["expected_old"]
        try:
            default_branch = self.github.get_repo(state.repository).default_branch
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"the default branch of {state.repository} could not be read: {exc}. This is "
                "not a transient GitHub failure; nothing was sent again. Fix the cause, then "
                "'unblock'"
            )
        planned_base = (
            str(records[1].target["base"])
            if records[1].kind is EffectKind.IMPLEMENTATION_PR
            else default_branch
        )
        if records[1].pending and planned_base != default_branch:
            # K2's base is checked before anything of the plan is sent, the
            # push included: a create is never issued onto a branch that is
            # no longer the default (ADR 0004 K2).
            return Phase.BLOCKED, (
                self._default_branch_changed_text(planned_base, default_branch)
                + f". The journaled plan opens the PR onto {planned_base!r}, so the controller "
                "sends nothing more for it: no push and no PR create (an attempt sent before "
                f"the change may already show on GitHub). Make {planned_base!r} the default "
                "branch again, then 'unblock'; otherwise this plan cannot complete, and the "
                "issue's PR is opened by hand"
            )
        for index, record in enumerate(records):
            if record.stage == Stage.CONFLICT:
                return Phase.BLOCKED, self._analyze_conflict_text(record)
            if not record.pending:
                continue
            try:
                if record.kind is EffectKind.ADOPT_PR:
                    drift = self._adopted_head_problem(
                        str(record.target["pr_url"]), candidate, expected_old, default_branch
                    )
                    if drift:
                        return Phase.BLOCKED, drift
                op = operation_for(
                    record,
                    self.github,
                    transport=self._git_transport() if record.kind is EffectKind.PUSH else None,
                    default_branch=default_branch,
                )
                driven = drive(record, op, self._persist_effect)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return Phase.BLOCKED, (
                    f"{record.describe()} could not be reconciled with GitHub: {exc}. This is "
                    "not a transient failure (authentication, permissions, or malformed "
                    "data); nothing was sent again. Fix the cause, then 'unblock'"
                )
            records[index] = driven.record
            if driven.record.stage == Stage.CONFLICT:
                return Phase.BLOCKED, self._analyze_conflict_text(driven.record)
        observed = records[1].observed
        if observed is None:
            raise StateError(f"{records[1].describe()} completed without an observed PR")
        return self._bind_published_pr(
            parse_pr_url(str(observed["url"])).canonical,
            branch=branch,
            candidate=candidate,
            expected_old=expected_old,
            default_branch=default_branch,
            adopted=records[1].kind is EffectKind.ADOPT_PR,
        )

    def _adopted_head_problem(
        self, url: str, candidate: str, expected_old: str | None, default_branch: str
    ) -> str:
        """Before K3 writes: the PR to adopt is onto the default branch, at the candidate, or "".

        The adoption binds the issue's marker to the PR's code, so it is
        written only once GitHub shows that code to be the candidate, on a
        PR that still targets the default branch. A head still at the
        branch's old value is GitHub catching up with the push
        (:class:`GitHubUnavailableError`, for 'resume'); any other head is
        something else pushed to the branch, and blocks, as does another
        base.
        """
        pr = self.github.get_pr(url)
        if pr.base_ref != default_branch:
            return (
                f"PR {url} targets {pr.base_ref!r}, not the default branch {default_branch!r}: "
                "it was retargeted, or the default branch changed, after the plan was saved, "
                "so the controller does not adopt it. Nothing was written to the PR. Retarget "
                "it onto the default branch, then 'unblock'"
            )
        if pr.head_sha == candidate:
            return ""
        if expected_old is not None and pr.head_sha == expected_old:
            raise GitHubUnavailableError(
                f"PR {url} is still headed at {expected_old} after the push of {candidate}; "
                "GitHub has not caught up yet, and 'resume' reads it again before adopting it"
            )
        return (
            f"PR {url} is headed at {pr.head_sha or 'nothing readable'}, not at the candidate "
            f"{candidate} the controller pushed: something else moved its branch, so the "
            "controller does not adopt it. Nothing was written to the PR. Inspect the branch, "
            "then 'unblock'"
        )

    def _bind_published_pr(
        self,
        url: str,
        *,
        branch: str,
        candidate: str,
        expected_old: str | None,
        default_branch: str,
        adopted: bool,
    ) -> tuple[Phase, str]:
        """Read the published PR back and bind it (#161): the identity the next entry finds.

        Read the way the entry reads it: one complete listing of the open
        PRs, in which exactly one carries this issue's implementation
        marker, and it is the PR the record observed, open, headed at the
        candidate on ``branch`` of this repository and based on the default
        branch. A head still at the branch's old value is GitHub catching up
        (transient); any other mismatch blocks.
        """
        state = self._require_state()
        issue = parse_issue_url(state.current_issue_url)
        try:
            holder = self._implementation_prs(issue).exactly_one()
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            return Phase.BLOCKED, (
                f"PR {url} was published for issue #{issue.number}, but the open PRs could not "
                f"be read back as its one implementation: {exc}. Inspect the PRs carrying the "
                "issue's marker, then 'unblock'"
            )
        pr = holder.obj
        found = parse_pr_url(pr.url).canonical
        problems = []
        if found != url:
            problems.append(f"the open PR carrying the marker is {found}")
        if not pr.is_open:
            problems.append(f"it is {pr.state}")
        if pr.head_repository and pr.head_repository.lower() != state.repository.lower():
            problems.append(f"it is headed in {pr.head_repository}")
        if pr.head_ref != branch:
            problems.append(f"it is headed at branch {pr.head_ref!r}")
        if pr.base_ref != default_branch:
            problems.append(f"it is based on {pr.base_ref!r}, not {default_branch!r}")
        if not problems and pr.head_sha != candidate:
            if expected_old is not None and pr.head_sha == expected_old:
                raise GitHubUnavailableError(
                    f"PR {url} is still headed at {expected_old} after the push of "
                    f"{candidate}; 'resume' reads it back again"
                )
            problems.append(f"it is headed at {pr.head_sha or 'nothing readable'}")
        if problems:
            return Phase.BLOCKED, (
                f"PR {url} does not read back as the implementation the controller published "
                f"({candidate} on {branch!r} onto {default_branch!r}): {'; '.join(problems)}. "
                "The controller binds only a PR it verified; inspect it, then 'unblock'"
            )
        state.current_pr_url = url
        state.current_head_sha = candidate
        state.current_base_ref = pr.base_ref
        state.current_merge_base_sha = ""  # bound at REVIEW entry
        state.current_branch = branch
        state.review_round = 0
        state.open_findings = []
        state.prior_findings = []
        state.review_history = []
        state.last_review_result = ""
        state.reviewed_pr_url = ""
        state.reviewed_head_sha = ""
        state.reviewed_base_ref = ""
        state.reviewed_merge_base_sha = ""
        self._analyze_entry = None
        self._analyze_candidate = ""
        nxt = self._next_phase(Phase.ANALYZE_EXECUTE, {})
        return nxt, (
            f"pushed {candidate[:12]} to {branch} and {'adopted' if adopted else 'opened'} PR "
            f"{url}; ANALYZE_EXECUTE -> {nxt.value}"
        )

    def _finish_analyze_entry(self, plan: StepPlan) -> StepOutcome:
        """Complete ANALYZE_EXECUTE from its persisted plan, launching nothing (#161)."""
        state = self._require_state()
        nxt, message = self._complete_analyze()
        if nxt == Phase.BLOCKED:
            return self._block(Phase.ANALYZE_EXECUTE, plan, message)
        validate_transition(Phase.ANALYZE_EXECUTE, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.ANALYZE_EXECUTE,
            plan=plan,
            message=f"{message} (completed from the persisted ANALYZE_EXECUTE plan; no agent "
            "launched)",
        )

    def _analyze_conflict_text(self, record: EffectRecord) -> str:
        state = self._require_state()
        if record.kind is EffectKind.PUSH:
            expected = record.precondition["expected_old"]
            repair = (
                f"put the branch back at {expected}"
                if expected is not None
                else "delete the branch, which did not exist when the controller planned the push"
            )
        else:
            repair = "close or repair the PR(s) named above that the controller did not write"
        return (
            f"{record.reason}. The controller never force-pushes, duplicates or chooses "
            f"between such objects; the run stays on issue {state.current_issue_url}. "
            f"Inspect GitHub, {repair}, then 'unblock': the record is reconciled again "
            "within its attempt bound"
        )

    def _check_review_result(self, payload: dict) -> ReviewResult:
        """The controller's half of the REMOTE REVIEW schema: the round's comment (#162).

        Checked before the result is accepted, so a refusal is corrected (the
        reviewer is asked again) before anything is posted. ``round`` and
        ``reviewed_head_sha`` are cross-checks against the round and the HEAD
        the controller bound; a mismatch is corrected, never followed (ADR
        0004 D8.1). The comment the controller renders from the result, with
        its own binding line and marker, must fit GitHub's comment limit
        (D8.6) and pass the credential and mention rules as a whole (D8.2,
        D8.3, :func:`review_comment_problem`): fields that pass one by one
        can still join into a credential shape, or leave a fence that a
        later field closes, so the mention after it is no longer code.
        """
        state = self._require_state()
        res = ReviewResult.from_payload(payload)
        expected_round = state.review_round + 1
        head = state.current_head_sha.lower()
        if res.round != expected_round:
            raise ControlResultValidationError(
                f"REVIEW: field 'round' is {res.round}, but this is review round "
                f"{expected_round}; report the round you were given"
            )
        if res.reviewed_head_sha != head:
            raise ControlResultValidationError(
                f"REVIEW: field 'reviewed_head_sha' is {res.reviewed_head_sha}, but the "
                f"controller bound this round to HEAD {head}; review that HEAD and report it"
            )
        body = review_comment_body(res, head, state.current_base_ref, state.current_merge_base_sha)
        if len(body) > MAX_BODY_CHARS:
            raise ControlResultValidationError(
                f"REVIEW: the review comment rendered from this result is {len(body)} "
                f"characters, over GitHub's limit of {MAX_BODY_CHARS}; nothing was posted. "
                "Shorten the prose sections or the findings and re-emit the CONTROL_RESULT"
            )
        problem = review_comment_problem(
            res, head, state.current_base_ref, state.current_merge_base_sha
        )
        if problem:
            raise ControlResultValidationError(f"REVIEW: {problem}")
        return res

    def _apply_review(self, res: ReviewResult) -> tuple[Phase, str]:
        """Plan, post and complete the round's review comment from the accepted result (#162).

        The result was checked before it was accepted
        (:meth:`_check_review_result`): it is this round's, of the bound
        HEAD, and its comment renders within the limit and the policy. A
        precondition read then finds no comment carrying the round's marker
        at the bound revision; one would be a comment the controller did not
        journal, and blocks with nothing planned or posted (ADR 0004 D9.6).
        The comment (K4) is planned and saved with the completion context
        (the round's verdict and findings) in one save, and
        :meth:`_complete_review` does the rest from what was saved, as a
        later entry would.
        """
        state = self._require_state()
        head = state.current_head_sha.lower()
        base = state.current_base_ref
        merge_base = state.current_merge_base_sha
        if res.round != state.review_round + 1 or res.reviewed_head_sha != head:
            raise StateError("a REVIEW result was applied without the check that accepted it")
        if not base:
            raise VerificationError(
                f"REVIEW round {res.round} was launched without a bound base branch; the "
                "review cannot be recorded against the diff it decided on"
            )
        if not merge_base:
            raise VerificationError(
                f"REVIEW round {res.round} was launched without a bound merge base; the "
                "review cannot be recorded against the diff it decided on"
            )
        pr_ref = parse_pr_url(state.current_pr_url)
        try:
            holder = self._review_comments(pr_ref, res.round, head, base, merge_base).at_most_one()
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"the comments of PR {pr_ref.canonical} could not be read before the comment "
                f"of review round {res.round} was posted: {exc}. This is not a transient "
                "GitHub failure; nothing was posted. Fix the cause, then 'unblock'"
            )
        except ClaimConflictError as exc:
            return Phase.BLOCKED, (
                f"{exc}. The controller posts this round's comment itself and never chooses "
                "between comments it did not post; nothing was posted. Remove the comments "
                "or their markers, then 'unblock'"
            )
        if holder is not None:
            return Phase.BLOCKED, self._unjournaled_review_text(holder.obj.url)
        canonical = pr_ref.canonical
        marker = render_review_marker(
            res.round, head, base, merge_base, res.needs_fix_round, [f.id for f in res.findings]
        )
        record = EffectRecord.plan(
            0,
            EffectKind.REVIEW_COMMENT,
            EffectOwner(
                run_id=state.run_id,
                phase=Phase.REVIEW,
                issue_url=state.current_issue_url,
                pr_url=state.current_pr_url,
                transaction_id="",
            ),
            identity={"pr_url": canonical, "marker": marker},
            target={"pr_url": canonical},
            precondition={"absent": True},
            payload={"body": review_comment_body(res, head, base, merge_base)},
        )
        context = ReviewContext(
            state.current_issue_url,
            state.current_pr_url,
            res.round,
            res.needs_fix_round,
            tuple(f.to_dict() for f in res.findings),
            dict(res.sections),
        )
        state.effect_records = [record.to_dict()]
        state.completion_context = context.to_dict()
        # Strictly validated exactly as a later load will validate it: a plan
        # that fails is never written (D4.6).
        state.phase_effects()
        self._save()
        return self._complete_review()

    def _finish_review_entry(self, plan: StepPlan) -> StepOutcome:
        """Complete REVIEW from its persisted plan, launching nothing (#162)."""
        state = self._require_state()
        try:
            nxt, message = self._complete_review()
        except VerificationError as exc:
            self._record_verification_failure(Phase.REVIEW, exc)
            self._save()
            raise
        if nxt == Phase.BLOCKED:
            return self._block(Phase.REVIEW, plan, message)
        validate_transition(Phase.REVIEW, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.REVIEW,
            plan=plan,
            message=f"{message} (completed from the persisted REVIEW plan; no reviewer launched)",
        )

    def _review_conflict_text(self, record: EffectRecord) -> str:
        state = self._require_state()
        return (
            f"{record.reason}. The controller never edits, duplicates or chooses between "
            f"such comments; the round is not recorded. Inspect PR {state.current_pr_url}, "
            "remove the comment(s) named above that the controller did not post, or their "
            "markers, then 'unblock': the record is reconciled again within its attempt bound"
        )

    def _complete_review(self) -> tuple[Phase, str]:
        """Finish REVIEW from the persisted plan: post the comment, then judge the round.

        The same code for the step that saved the plan and for every later
        entry (journal first): the K4 record is reconciled and issued at most
        once, and the comment it reads back (exactly one comment carrying
        the round's marker, with the planned body) is the round's handoff
        artifact. A conflict or a conclusive GitHub failure blocks with the
        record as persisted; an unavailable GitHub or a write whose outcome
        is not readable yet propagates for 'resume'.

        Then the post-review re-read judges the round from the persisted
        verdict and findings, never from the comment's text, against the
        revision the marker binds (an ``unblock`` may have rebound the
        state's): a closed or merged PR refuses the round
        (:class:`VerificationError`, nothing consumed); a HEAD, base or merge
        base that moved makes it stale; otherwise its verdict decides. The
        save that consumes the round drops the plan with it.
        """
        state = self._require_state()
        effects = state.phase_effects()
        context = effects.context
        if not isinstance(context, ReviewContext) or len(effects.records) != 1:
            raise StateError("REVIEW has no persisted review comment plan to complete")
        record = effects.records[0]
        if record.stage == Stage.CONFLICT:
            return Phase.BLOCKED, self._review_conflict_text(record)
        if record.pending:
            try:
                record = drive(record, ReviewCommentOp(self.github), self._persist_effect).record
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return Phase.BLOCKED, (
                    f"{record.describe()} could not be reconciled with GitHub: {exc}. This is "
                    "not a transient failure (authentication, permissions, or malformed "
                    "data); nothing was sent again. Fix the cause, then 'unblock'"
                )
            if record.stage == Stage.CONFLICT:
                return Phase.BLOCKED, self._review_conflict_text(record)
        if record.observed is None:  # pragma: no cover - terminal and not a conflict
            raise StateError(f"{record.describe()} is settled without an observed comment")
        comment_url = parse_comment_url(record.observed["url"]).canonical
        claim = scan(REVIEW, record.marker).claims[0]
        expected_head = claim.reviewed_head_sha
        expected_base = claim.reviewed_base_ref or ""
        expected_merge_base = claim.reviewed_merge_base_sha or ""
        round_ = context.round
        needs_fix_round = context.needs_fix_round

        # The post-review re-read comes before anything is recorded. It can
        # still refuse the round (the PR closed or merged under the reviewer),
        # and a refused round consumes neither `review_round` nor a
        # `review_history` entry: the step persists a failed verification
        # together with whatever this method had changed by then.
        latest = self._require_open_pr()
        moved = ""
        latest_merge_base = ""
        if latest.head_sha != expected_head:
            moved = f"PR HEAD moved to {latest.head_sha[:12]}"
        elif latest.base_ref != expected_base:
            moved = f"PR base changed from {expected_base!r} to {latest.base_ref!r}"
        else:
            # Same HEAD and base name: the diff is still the reviewed one
            # only if the base is still the history it was computed against
            # (#96). The REVIEW entry re-binds all three on the stale path,
            # so the merge base is read only where it decides the outcome.
            latest_merge_base = self._merge_base_of(latest)
            if latest_merge_base != expected_merge_base:
                moved = (
                    f"PR merge base moved from {expected_merge_base[:12]} to "
                    f"{latest_merge_base[:12]} (base {expected_base!r} was rewritten under "
                    "its name)"
                )

        # Valid review lifecycle: round is consumed regardless of the outcome.
        # The round is bound to the revision it decided on -- the PR the
        # comment was verified to belong to, the HEAD, the base and the
        # merge base -- and the merge gate later requires all four, not the
        # HEAD alone.
        state.review_round = round_
        state.reviewed_pr_url = parse_pr_url(state.current_pr_url).canonical
        state.reviewed_head_sha = expected_head
        state.reviewed_base_ref = expected_base
        state.reviewed_merge_base_sha = expected_merge_base
        state.last_review_comment_url = comment_url
        state.last_review_needs_fix = needs_fix_round
        # Findings are agent-authored text persisted in plain `state.json` and
        # rendered into the next FIX prompt, so they cross the same redaction
        # boundary as the run log.
        findings = [redact_dict(dict(f)) for f in context.findings]
        # The plan is complete: the save that consumes the round drops it, so
        # that a later entry of REVIEW (a stale round's re-review, or an
        # unblock after a loop stop) reviews the next round afresh.
        state.drop_phase_effects()
        if moved:
            state.current_head_sha = latest.head_sha
            state.current_base_ref = latest.base_ref
            state.current_merge_base_sha = latest_merge_base
            state.last_review_result = "stale"
            state.open_findings = []
            # The round is consumed (it is a completed review, and consuming
            # it keeps the cap in force however often the revision moves),
            # but its findings are not lost: no fixer will resolve them, so
            # the next review is told to re-check them at the actual HEAD.
            # The stale round's verdict replaces any earlier carry, because
            # that reviewer was shown the earlier findings and re-raised
            # the ones that still applied.
            state.prior_findings = findings
            self._record_review(round_, expected_head, RESULT_STALE, findings)
            carried = (
                f"; its {len(findings)} finding(s) are carried to that review to re-check"
                if findings
                else ""
            )
            nxt = self._next_phase(
                Phase.REVIEW,
                {"needs_fix_round": needs_fix_round, "head_changed_after_review": True},
            )
            return nxt, (
                f"review round {round_} completed for {expected_head[:12]} but {moved} "
                f"during the review; REVIEW -> {nxt.value} of the latest revision{carried}"
            )
        # The revision is the bound one. It is written back because an
        # unblock into this plan rebinds the HEAD and base and leaves the
        # merge base for the entry to read, and a plan completes unbound.
        state.current_head_sha = latest.head_sha
        state.current_base_ref = latest.base_ref
        state.current_merge_base_sha = latest_merge_base
        # A completed round of the actual revision decides about the carried
        # findings: the reviewer was shown them and re-raised, under this
        # round's ids, the ones that still apply.
        state.prior_findings = []
        if needs_fix_round:
            state.last_review_result = "needs_fix"
            state.open_findings = findings
            self._record_review(round_, expected_head, RESULT_NEEDS_FIX, findings)
            workflow_stagnation = stagnation_reason(
                state.review_history,
                self.config.workflow.stagnation_identical_rounds,
                self.config.workflow.stagnation_unchanged_count_rounds,
            )
            decision = evaluate_replan_policy(
                has_actionable_findings=True,
                current_review_round=round_,
                review_history=state.review_history,
                escalation_count=state.escalation_count,
                config=self.config.review.replan,
                workflow_stagnation_reason=workflow_stagnation,
            )
            if decision.action == "block_for_human":
                return Phase.BLOCKED, self._loop_block_reason(
                    self._replan_refusal(decision) + ". Human intervention is required"
                )
            replan = decision.action == "replan"
            nxt = self._next_phase(Phase.REVIEW, {"needs_fix_round": True, "replan": replan})
            if replan:
                # The decision binds the PR and the branch as well as the HEAD,
                # and a journal missing any of them is refused on load; refuse
                # here, where nothing has been recorded yet, rather than
                # persist a decision that can only be replayed as corruption.
                unbound = ""
                if not state.current_branch:
                    unbound = "the reviewed branch is not recorded in controller state"
                try:
                    decision_pr = parse_pr_url(state.current_pr_url).canonical
                except ConfigurationError as exc:
                    unbound = f"the reviewed PR URL is unusable ({exc})"
                try:
                    decision_issue = parse_issue_url(state.current_issue_url).canonical
                except ConfigurationError as exc:  # pragma: no cover - parsed by the prompt
                    unbound = f"the reviewed issue URL is unusable ({exc})"
                if unbound:
                    return Phase.BLOCKED, self._loop_block_reason(
                        f"review round {round_}: {len(findings)} finding(s); controller "
                        f"policy triggered REPLAN_REEXECUTE ({decision.reason}), but {unbound}, "
                        "so the replan decision cannot bind the revision it was made on. Human "
                        "intervention is required"
                    )
                # Only the decision is recorded here, together with the issue,
                # the PR, the revision it was made on, the base that revision
                # was reviewed against and the merge base the reviewed diff
                # was computed from (all bound at REVIEW entry and verified
                # non-empty above). The checkpoint and the transaction id are
                # created by `_prepare_replan`, inside the REPLAN_REEXECUTE
                # step that owns them.
                state.replan_transaction = ReplanTransaction(
                    stage=ReplanStage.PENDING,
                    issue_url=decision_issue,
                    decision_pr_url=decision_pr,
                    decision_head_sha=expected_head,
                    decision_branch=state.current_branch,
                    decision_base_ref=expected_base,
                    decision_merge_base_sha=expected_merge_base,
                    escalation=decision.metadata or {"trigger": decision.reason},
                ).to_dict()
                return nxt, (
                    f"review round {round_}: {len(findings)} finding(s); controller policy "
                    f"triggered {nxt.value} ({decision.reason})"
                )
            stop = self._review_loop_stop_reason(round_)
            if stop:
                # Findings stay persisted for the human; no FIX is started.
                return Phase.BLOCKED, self._loop_block_reason(
                    f"review round {round_}: {len(findings)} finding(s), but {stop}"
                )
            return nxt, (
                f"review round {round_}: {len(findings)} finding(s); REVIEW -> {nxt.value}"
            )
        state.last_review_result = "clean"
        state.open_findings = []
        self._record_review(round_, expected_head, RESULT_CLEAN, findings)
        nxt = self._next_phase(Phase.REVIEW, {"needs_fix_round": False})
        return nxt, (
            f"review round {round_} clean for HEAD {expected_head[:12]}; REVIEW -> {nxt.value}"
        )

    @staticmethod
    def _replan_refusal(decision: ReplanDecision) -> str:
        """Human-readable text for a ``block_for_human`` replan decision."""
        if decision.reason == "replan_evidence_truncated":
            listed = (decision.metadata or {}).get("truncated_evidence_rounds")
            rounds = ", ".join(str(r) for r in listed) if isinstance(listed, list) else ""
            return (
                "Automatic reimplementation is refused (replan_evidence_truncated): the "
                f"persisted findings of review round(s) {rounds or '(unknown)'} are an "
                "incomplete copy of the review, so a replacement could never be required "
                "to consider every actionable finding. The PR is kept so its findings are "
                "not lost"
            )
        return f"Automatic reimplementation limit reached ({decision.reason})"

    def _record_review(self, round: int, head: str, result: str, findings: list[dict]) -> None:
        """Append the completed round to ``review_history``.

        Bounded twice over: one entry per round (rounds are capped by
        ``workflow.max_review_rounds`` and cleared per PR) and, inside an
        entry, at most ``MAX_PERSISTED_RESOLUTION_DIGESTS`` digests.
        """
        state = self._require_state()
        # A completed round replaces any stale entry with the same number
        # (never expected: rounds are strictly increasing per PR).
        state.review_history = [r for r in state.review_history if r.get("round") != round]
        state.review_history.append(
            review_record(
                round,
                head,
                result,
                findings,
                state.last_review_comment_url,
                utcnow_iso(),
            )
        )

    def _review_loop_stop_reason(self, completed_round: int) -> str:
        """Cap / stagnation verdict for a round that ended with findings."""
        wf = self.config.workflow
        state = self._require_state()
        reason = round_cap_reason(completed_round, wf.max_review_rounds, has_findings=True)
        if reason:
            return reason
        return stagnation_reason(
            state.review_history,
            wf.stagnation_identical_rounds,
            wf.stagnation_unchanged_count_rounds,
        )

    def _apply_fix(self, res: FixResult) -> tuple[Phase, str]:
        """Plan the FIX writes from the accepted result, save them, then complete (#163).

        The result was checked before it was accepted
        (:meth:`_check_fix_result`): the resolutions cover the open findings
        exactly and the candidate is the worktree's HEAD, the reviewed HEAD
        or a fast-forward of it. A precondition read then finds GitHub as the
        entry observed it: the PR open at the reviewed HEAD (a HEAD past it
        is a push the controller did not make: ``FIX -> REVIEW``, nothing
        sent), each open finding's follow-up issue as the entry recorded it,
        and each issue a deferral names open in this repository with a body
        the marker block can be appended to; anything else blocks with
        nothing planned or sent (ADR 0004 §2.5). The follow-up issues to
        create (K5, in result order), the marker appends (K6, one per
        handed-over issue, its findings' markers in result order) and the
        push (K1, last: the commit point; none when nothing was committed)
        are planned and saved with the completion context in one save, and
        :meth:`_complete_fix` does the rest from what was saved, as a later
        entry would.
        """
        state = self._require_state()
        reviewed = state.reviewed_head_sha.lower()
        candidate = self._fix_candidate
        if not candidate or candidate != res.head_sha or res.previous_head_sha != reviewed:
            raise StateError("a FIX result was applied without the candidate its check accepted")
        observation = state.phase_effects().observation
        if observation is None or observation.phase != Phase.FIX:
            raise StateError("a FIX result was applied without the observation of its entry")
        pr = self._require_open_pr()
        if pr.head_sha.lower() != reviewed:
            return self._fix_drift(
                pr,
                f"PR HEAD {pr.head_sha[:12]} moved past the reviewed HEAD {reviewed[:12]} while "
                "the fixer ran (a push the controller did not make; it does not infer which "
                "findings it resolved), so nothing was created, appended or pushed",
            )
        pr_ref = parse_pr_url(state.current_pr_url)
        pr_url = pr_ref.canonical
        open_ids = [str(f["id"]) for f in state.open_findings]
        ref = f"refs/heads/{pr.head_ref}"
        try:
            own, _ = self._fix_follow_ups(pr_ref, open_ids)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"the follow-up issues of PR {pr_url} could not be read before the FIX plan "
                f"was saved: {exc}. This is not a transient GitHub failure; nothing was "
                "created, appended or pushed. Fix the cause, then 'unblock'"
            )
        except ClaimConflictError as exc:
            return Phase.BLOCKED, (
                f"{exc}. The controller never chooses between follow-up issues; nothing was "
                "created, appended or pushed. Close or repair the extra or unreadable "
                "issue(s) so at most one remains per finding, then 'unblock'"
            )
        observed = self._fix_observed_objects(pr_ref, open_ids, own)
        if observed != dict(observation.objects) or dict(observation.refs) != {ref: reviewed}:
            return Phase.BLOCKED, self._unjournaled_fix_text(observation, observed, ref, reviewed)
        new: list[FindingResolution] = []
        appends: dict[str, list[str]] = {}
        for r in res.resolutions:
            if r.resolution != "follow_up_created" or r.finding_id in own:
                continue
            if r.new_follow_up:
                new.append(r)
            else:
                url = parse_issue_url(r.follow_up_issue_url).canonical
                appends.setdefault(url, []).append(r.finding_id)
        bases: dict[str, str] = {}
        try:
            watermark = self.github.latest_issue_number(state.repository) if new else 0
            for url, fids in appends.items():
                issue = self.github.get_issue(url)
                deferred = ", ".join(fids)
                if not issue.is_open or not parse_issue_url(issue.url or url).same_repository(
                    state.repository
                ):
                    return Phase.BLOCKED, (
                        f"follow-up issue {url}, which {deferred} is deferred to, is "
                        f"{issue.state} or not in {state.repository}: the controller appends "
                        "a finding's marker to an open issue of this repository only. Nothing "
                        "was created, appended or pushed. Reopen it, or 'unblock' to launch "
                        "the fixer afresh"
                    )
                block = "\n".join(render_follow_up_marker(pr_url, fid) for fid in fids)
                problem = append_problem(url, FOLLOW_UP, compose_append(issue.body, block))
                if problem:
                    return Phase.BLOCKED, (
                        f"the markers of {deferred} cannot be appended to {url}: {problem}. "
                        "Nothing was created, appended or pushed"
                    )
                bases[url] = issue.body
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"the issues the FIX plan writes to could not be read before it was saved: "
                f"{exc}. This is not a transient GitHub failure; nothing was created, "
                "appended or pushed. Fix the cause, then 'unblock'"
            )
        owner = EffectOwner(
            run_id=state.run_id,
            phase=Phase.FIX,
            issue_url=state.current_issue_url,
            pr_url=state.current_pr_url,
            transaction_id="",
        )
        records: list[EffectRecord] = []
        sources: dict[str, str | int] = {
            fid: FOLLOW_UP_SOURCE_ENTRY
            for fid in own
            if any(r.finding_id == fid for r in res.resolutions)
        }
        for r in new:
            marker = render_follow_up_marker(pr_url, r.finding_id)
            sources[r.finding_id] = len(records)
            records.append(
                EffectRecord.plan(
                    len(records),
                    EffectKind.FOLLOW_UP_ISSUE,
                    owner,
                    identity={"repository": state.repository, "marker": marker},
                    target={"repository": state.repository},
                    precondition={"absent": True, "watermark": watermark},
                    payload={
                        "title": r.follow_up_title,
                        "body": follow_up_issue_body(r.follow_up_body, pr_url, r.finding_id),
                    },
                )
            )
        for url, fids in appends.items():
            markers = [render_follow_up_marker(pr_url, fid) for fid in fids]
            block = "\n".join(markers)
            for fid in fids:
                sources[fid] = len(records)
            records.append(
                EffectRecord.plan(
                    len(records),
                    EffectKind.FOLLOW_UP_APPEND,
                    owner,
                    identity={"issue_url": url, "markers": markers},
                    target={"issue_url": url},
                    precondition={"base_sha256": sha256_text(bases[url])},
                    payload={"body": compose_append(bases[url], block), "block": block},
                )
            )
        if candidate != reviewed:
            records.append(
                EffectRecord.plan(
                    len(records),
                    EffectKind.PUSH,
                    owner,
                    identity={
                        "repository": state.repository,
                        "ref": ref,
                        "candidate_sha": candidate,
                    },
                    target={"repository": state.repository, "ref": ref},
                    precondition={"expected_old": reviewed, "base_sha": reviewed},
                    payload={"sha": candidate},
                )
            )
        context = FixContext(
            state.current_issue_url,
            state.current_pr_url,
            state.review_round,
            tuple(
                {
                    "finding_id": r.finding_id,
                    "resolution": r.resolution,
                    # Agent-authored text persisted in plain `state.json` and
                    # shown to the next reviewer: the same redaction boundary
                    # as the run log.
                    "rationale": redact(r.rationale),
                    "commit_sha": r.commit_sha,
                    "follow_up_source": sources.get(r.finding_id),
                }
                for r in res.resolutions
            ),
        )
        problem = plan_size_problem(records, context)
        if problem:
            return Phase.BLOCKED, (
                f"the FIX plan cannot be saved: {problem}. Nothing was created, appended or "
                "pushed. Shorten the issue bodies it appends to, then 'unblock'"
            )
        state.effect_records = [record.to_dict() for record in records]
        state.completion_context = context.to_dict()
        # Strictly validated exactly as a later load will validate it, against
        # the entry observation the pre-launch save persisted: a plan that
        # fails is never written (D4.6).
        state.phase_effects()
        self._fix_candidate = ""
        self._save()
        return self._complete_fix()

    def _finish_fix_entry(self, plan: StepPlan) -> StepOutcome:
        """Complete FIX from its persisted plan, launching nothing (#163)."""
        state = self._require_state()
        try:
            nxt, message = self._complete_fix()
        except VerificationError as exc:
            self._record_verification_failure(Phase.FIX, exc)
            self._save()
            raise
        if nxt == Phase.BLOCKED:
            return self._block(Phase.FIX, plan, message)
        validate_transition(Phase.FIX, nxt)
        state.phase = nxt
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.FIX,
            plan=plan,
            message=f"{message} (completed from the persisted FIX plan; no fixer launched)",
        )

    def _fix_drift(self, pr: PRInfo, what: str) -> tuple[Phase, str]:
        """FIX -> REVIEW of the actual revision: the fix is not recorded (#163).

        The plan is dropped with the phase it belongs to. What it already
        sent stays on GitHub: a follow-up issue it created or a marker it
        appended is an earlier round's deferral to the next fixer.
        """
        state = self._require_state()
        state.last_fix_resolutions = []
        state.drop_phase_effects()
        nxt, _, carried = self._stale_revision(Phase.FIX, pr)
        return nxt, f"{what}; FIX -> {nxt.value} of the actual HEAD, no fix recorded{carried}"

    def _fix_conflict(self, record: EffectRecord) -> tuple[Phase, str]:
        """A conflicting FIX record: drift when the PR moved, otherwise BLOCKED.

        A push refused by its lease is the case to tell apart: a human push
        to the PR branch before the controller's is the same drift a later
        entry would route (``FIX -> REVIEW``), never a reason to force it.
        """
        state = self._require_state()
        if record.kind is EffectKind.PUSH:
            pr = self._require_open_pr()
            head = pr.head_sha.lower()
            if head not in (str(record.precondition["expected_old"]), str(record.payload["sha"])):
                return self._fix_drift(
                    pr,
                    f"{record.describe()} was refused and PR HEAD is {head[:12]} (a push the "
                    "controller did not make; it does not infer which findings it resolved)",
                )
            repair = (
                f"leave the PR branch at the reviewed HEAD {record.precondition['expected_old']} "
                f"or put it at the candidate {record.payload['sha']}, and fix what refused the "
                "push"
            )
        elif record.kind is EffectKind.FOLLOW_UP_ISSUE:
            repair = (
                "reopen the follow-up issue the controller created, or close or unmark the "
                "issue(s) named above that it did not create"
            )
        else:
            repair = (
                f"put the body of {record.target['issue_url']} back to a state the markers can "
                "be appended to, or unmark the issue(s) named above that the controller did not "
                "write"
            )
        return Phase.BLOCKED, (
            f"{record.reason}. The controller never force-pushes, duplicates or chooses between "
            f"such objects; the fix of review round {state.review_round} is not recorded. "
            f"Inspect GitHub, {repair}, then 'unblock': the record is reconciled again within "
            "its attempt bound"
        )

    def _fix_source_url(
        self,
        finding_id: str,
        source: str | int,
        records: Sequence[EffectRecord],
        observation: EntryObservation,
    ) -> str:
        """The follow-up issue a deferred finding's source names, as the plan read it back."""
        state = self._require_state()
        marker = render_follow_up_marker(parse_pr_url(state.current_pr_url).canonical, finding_id)
        if source == FOLLOW_UP_SOURCE_ENTRY:
            url = observation.objects.get(marker)
        else:
            assert isinstance(source, int)
            record = records[source]
            url = (record.observed or {}).get("url")
        if not url:
            raise StateError(f"the follow-up of {finding_id} has no issue its source observed")
        return str(url)

    def _fix_follow_up_problem(
        self,
        context: FixContext,
        records: Sequence[EffectRecord],
        observation: EntryObservation,
        *,
        reused_only: bool,
    ) -> str:
        """Why a deferred finding is not exactly one open issue's follow-up, or "".

        One complete listing of the open issues: each deferred finding (or,
        with ``reused_only``, each finding reusing the follow-up its entry
        found) has exactly one open issue carrying its marker, and it is the
        issue its source names. A closed follow-up, or a second issue marked
        for the same finding, is never resolved by choosing.
        """
        pr_ref = parse_pr_url(context.pr_url)
        follow_ups = self._follow_up_issues(pr_ref)
        problems = []
        for resolution in context.resolutions:
            source = resolution["follow_up_source"]
            if source is None or (reused_only and source != FOLLOW_UP_SOURCE_ENTRY):
                continue
            fid = resolution["finding_id"]
            expected = self._fix_source_url(fid, source, records, observation)
            try:
                holder = follow_ups.claimants(
                    (pr_ref.identity, fid), _finding_what(pr_ref, fid)
                ).exactly_one()
            except ClaimConflictError as exc:
                problems.append(f"{exc} (its follow-up is {expected})")
                continue
            if not same_issue_url(holder.obj.url, expected):
                problems.append(
                    f"the open follow-up issue of {fid} is {holder.obj.url}, not {expected}"
                )
        return "; ".join(problems)

    def _complete_fix(self) -> tuple[Phase, str]:
        """Finish FIX from the persisted plan: create, append, push, read back (#163).

        The same code for the step that saved the plan and for every later
        entry (journal first). A PR HEAD that is neither the reviewed HEAD nor
        the planned candidate is a push the controller did not make
        (``FIX -> REVIEW``); the candidate itself is the controller's own push,
        never a reason to review first. A follow-up issue reused from the
        entry is re-read before anything is sent. Each record is then
        reconciled against GitHub and issued at most once, in plan order, so
        the push (the commit point) is never issued before every follow-up
        is on GitHub; a push refused because the branch moved is the same
        drift. Then every deferral is read back as the one open issue
        carrying its finding's marker, and the PR as headed at the candidate.
        A conflict or a conclusive failure blocks with the records as
        persisted; an unavailable GitHub, a write whose outcome is not
        readable yet, or a PR head GitHub has not moved yet, propagates for
        'resume'.
        """
        state = self._require_state()
        effects = state.phase_effects()
        context = effects.context
        observation = effects.observation
        if not isinstance(context, FixContext) or observation is None:
            raise StateError("FIX has no persisted plan to complete")
        records = list(effects.records)
        reviewed = state.reviewed_head_sha.lower()
        push = records[-1] if records and records[-1].kind is EffectKind.PUSH else None
        candidate = str(push.payload["sha"]) if push is not None else reviewed
        pr = self._require_open_pr()
        if pr.head_sha.lower() not in (reviewed, candidate):
            return self._fix_drift(
                pr,
                f"PR HEAD {pr.head_sha[:12]} is neither the reviewed HEAD {reviewed[:12]} nor "
                f"the candidate {candidate[:12]} the plan pushes (a push the controller did not "
                "make; it does not infer which findings it resolved)",
            )
        try:
            problem = self._fix_follow_up_problem(context, records, observation, reused_only=True)
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            problem = str(exc)
        if problem:
            return Phase.BLOCKED, (
                f"{problem}. A follow-up issue the fixer reused is no longer the one open issue "
                "carrying its finding's marker, and the controller never chooses another; "
                "nothing more was sent. Reopen it or close the extra issue, then 'unblock'"
            )
        default_branch = ""
        for index, record in enumerate(records):
            if record.stage == Stage.CONFLICT:
                return self._fix_conflict(record)
            if not record.pending:
                continue
            try:
                if record.kind is EffectKind.PUSH:
                    default_branch = self.github.get_repo(state.repository).default_branch
                op = operation_for(
                    record,
                    self.github,
                    transport=self._git_transport() if record.kind is EffectKind.PUSH else None,
                    default_branch=default_branch,
                )
                driven = drive(record, op, self._persist_effect)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return Phase.BLOCKED, (
                    f"{record.describe()} could not be reconciled with GitHub: {exc}. This is "
                    "not a transient failure (authentication, permissions, or malformed "
                    "data); nothing was sent again. Fix the cause, then 'unblock'"
                )
            records[index] = driven.record
            if driven.record.stage == Stage.CONFLICT:
                return self._fix_conflict(driven.record)
        try:
            problem = self._fix_follow_up_problem(context, records, observation, reused_only=False)
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            problem = str(exc)
        if problem:
            return Phase.BLOCKED, (
                f"{problem}. The follow-up issues of this FIX do not read back as the one open "
                "issue carrying each deferred finding's marker; the fix is not recorded. "
                "Reopen the controller's issue or close the extra one, then 'unblock'"
            )
        latest = self._require_open_pr()
        head = latest.head_sha.lower()
        if push is not None and head == reviewed:
            raise GitHubUnavailableError(
                f"PR {state.current_pr_url} is still headed at the reviewed HEAD {reviewed} "
                f"after the push of {candidate}; GitHub has not caught up yet, and 'resume' "
                "reads it back again"
            )
        if head != candidate:
            return self._fix_drift(
                latest,
                f"PR HEAD {head[:12]} is not the candidate {candidate[:12]} the controller "
                "verified (a push the controller did not make)",
            )
        state.last_fix_resolutions = [
            {
                "finding_id": r["finding_id"],
                "resolution": r["resolution"],
                "rationale": r["rationale"],
                "follow_up_issue_url": (
                    ""
                    if r["follow_up_source"] is None
                    else self._fix_source_url(
                        r["finding_id"], r["follow_up_source"], records, observation
                    )
                ),
                "commit_sha": r["commit_sha"],
            }
            for r in context.resolutions
        ]
        state.current_head_sha = latest.head_sha
        state.last_review_result = "fixed"
        # The plan is complete, and the save that leaves FIX drops it; the
        # open findings go last, because the plan resolves exactly them.
        state.drop_phase_effects()
        state.open_findings = []
        created = sum(1 for r in records if r.kind is EffectKind.FOLLOW_UP_ISSUE)
        appended = sum(1 for r in records if r.kind is EffectKind.FOLLOW_UP_APPEND)
        nxt = self._next_phase(Phase.FIX, {})
        moved = (
            f"pushed {candidate[:12]} over {reviewed[:12]}"
            if push is not None
            else f"nothing to push (HEAD stays {reviewed[:12]})"
        )
        return nxt, (
            f"FIX of review round {context.round}: {len(context.resolutions)} resolution(s), "
            f"{created} follow-up issue(s) created, {appended} marker append(s), {moved}; "
            f"FIX -> {nxt.value} (round {state.review_round + 1})"
        )

    def _apply_replan(self, res: ReplanReexecuteResult) -> tuple[Phase, str]:
        """Cross-check the agent's claims, then bind, supersede and activate.

        The CONTROL_RESULT is a claim, never the authority. Every field below
        is compared against the checkpoint the controller wrote *before* the
        agent ran, or against GitHub; a mismatch means the agent is describing
        a different world than the one the replan decision was made in, and
        this attempt is rejected.

        Acceptance itself is delegated to the same helpers ``_drive_replan``
        uses on ``resume`` — the marker on the replacement PR, not this
        payload, is what proves causality and carries the attestation. That is
        why a crash between the agent's write and this method cannot lower the
        bar: there is nothing here that recovery does not also check.
        """
        state = self._require_state()
        if not state.replan_transaction:
            raise StateError("REPLAN_REEXECUTE has no prepared transaction")
        txn = ReplanTransaction.from_dict(state.replan_transaction)
        if txn.stage is not ReplanStage.PREPARED:
            return self._blocked_replan(
                self._reject_replan(
                    txn,
                    "a REPLAN_REEXECUTE result arrived while the transaction was at stage "
                    f"{txn.stage.value!r}, which cannot accept one",
                )
            )
        claimed_url = parse_pr_url(res.replacement_pr_url).canonical
        mismatch = ""
        if not same_issue_url(res.issue_url, txn.issue_url):
            mismatch = f"issue_url {res.issue_url!r} does not match the replan issue"
        elif not same_pr_url(res.previous_pr_url, txn.source_pr_url):
            mismatch = f"previous_pr_url {res.previous_pr_url!r} does not match the checkpoint"
        elif res.previous_branch != txn.source_branch:
            mismatch = f"previous_branch {res.previous_branch!r} does not match the checkpoint"
        elif res.previous_head_sha != txn.source_head_sha:
            mismatch = f"previous_head_sha {res.previous_head_sha!r} does not match the checkpoint"
        elif res.execution_attempt != txn.expected_execution_attempt:
            mismatch = (
                f"execution_attempt must be {txn.expected_execution_attempt}, "
                f"got {res.execution_attempt}"
            )
        elif not res.tests_passed:
            mismatch = "the replacement reports tests_passed=false"
        elif res.historical_findings_considered < txn.evidence_finding_count:
            mismatch = (
                f"the replacement considered {res.historical_findings_considered} of the "
                f"{txn.evidence_finding_count} historical finding(s) the controller preserved"
            )
        if mismatch:
            return self._blocked_replan(
                self._reject_replan(
                    txn, f"REPLAN_REEXECUTE result rejected: {mismatch}", claimed_url
                )
            )
        refused = self._bind_replacement(txn, claimed_url=claimed_url)
        if refused is not None:
            return self._blocked_replan(refused)
        if not txn.is_bound:  # pragma: no cover - _bind_replacement rejects this
            return self._blocked_replan(
                self._reject_replan(txn, "no replacement PR is bound to this transaction")
            )
        if res.replacement_branch != txn.replacement_branch:
            return self._blocked_replan(
                self._reject_replan(
                    txn,
                    f"replacement branch mismatch: GitHub reports {txn.replacement_branch!r}, "
                    f"the agent claimed {res.replacement_branch!r}",
                    txn.replacement_pr_url,
                )
            )
        if res.replacement_head_sha != txn.replacement_head_sha:
            return self._blocked_replan(
                self._reject_replan(
                    txn,
                    f"replacement HEAD mismatch: GitHub reports {txn.replacement_head_sha}, "
                    f"the agent claimed {res.replacement_head_sha}",
                    txn.replacement_pr_url,
                )
            )
        # The marker is the authoritative attestation, so stdout is not allowed
        # to tell a different story about it: an internally inconsistent result
        # means the two numbers were not produced by one honest accounting, and
        # the controller cannot tell which (if either) is the real one.
        for field_name, claimed, attested in (
            (
                "historical_findings_considered",
                res.historical_findings_considered,
                txn.attested_findings_considered,
            ),
            (
                "unique_failure_constraints",
                res.unique_failure_constraints,
                txn.attested_unique_constraints,
            ),
        ):
            if claimed != attested:
                return self._blocked_replan(
                    self._reject_replan(
                        txn,
                        f"CONTROL_RESULT {field_name}={claimed} disagrees with the replan marker "
                        f"published on {txn.replacement_pr_url}, which attests {attested}",
                        txn.replacement_pr_url,
                    )
                )
        outcome = self._supersede_source(txn)
        if state.phase == Phase.BLOCKED:
            return Phase.BLOCKED, outcome.message
        # ``_supersede_source`` activated the replacement and already applied
        # REPLAN_REEXECUTE's one edge; the same decision is returned here.
        return self._next_phase(Phase.REPLAN_REEXECUTE, {}), outcome.message

    @staticmethod
    def _blocked_replan(outcome: StepOutcome) -> tuple[Phase, str]:
        """Adapt a persisted replan refusal to the verify/apply return type."""
        return Phase.BLOCKED, outcome.message

    def _reject_next_issue(self, reason: str, *, cause: BaseException) -> tuple[Phase, str]:
        """Bounded re-selection: persist the rejection, re-ask once, then BLOCKED.

        Used for a bad selection and for a transient GitHub failure while
        checking it (the same read may well succeed next time). Never used for
        conclusive GitHub failures — see :meth:`_apply_update_epic`.
        """
        state = self._require_state()
        # The reason quotes the verification failure (an issue title, `gh`
        # stderr) and is persisted and rendered into the next prompt.
        reason = redact(reason)
        state.next_issue_rejections.append(reason)
        n = len(state.next_issue_rejections)
        if n >= MAX_NEXT_ISSUE_SELECTIONS:
            return Phase.BLOCKED, (
                f"UPDATE_EPIC selected an unusable next issue {n} time(s) "
                f"(limit {MAX_NEXT_ISSUE_SELECTIONS}); last rejection: {reason}. The run "
                f"stays on issue {state.current_issue_url}; nothing was switched. Pick "
                "the next issue manually (a new run) or fix the EPIC."
            )
        raise VerificationError(
            f"{reason}. Not switching issues; the run stays in UPDATE_EPIC (selection "
            f"{n}/{MAX_NEXT_ISSUE_SELECTIONS}) — 'resume' to let the agent select again."
        ) from cause

    # -- UPDATE_EPIC: the controller publishes (ADR 0004 §2.5, D4.6, D4.7, D13.7) --

    def _progress_marker(self) -> str:
        state = self._require_state()
        return render_progress_marker(state.current_issue_url, state.current_pr_url)

    def _update_epic_context(self) -> UpdateEpicContext | None:
        """The persisted UPDATE_EPIC completion context, or ``None``."""
        context = self._require_state().phase_effects().context
        return context if isinstance(context, UpdateEpicContext) else None

    def _update_epic_request(self) -> UpdateEpicRequest:
        """Which result the next UPDATE_EPIC launch asks for (D4.7, D13.7).

        ``FULL`` until the progress comment is published. Afterwards the
        agent is launched again only for an input a persisted rejection
        voided, and asked for that input alone: the selection (with the
        roadmap section while merges are uncounted, because a new selection
        of ``null`` may require one) or the section. The selection asked
        after adopting a legacy comment (D13.7) follows the same rule.
        """
        state = self._require_state()
        if state.mode != WorkflowMode.REMOTE or state.phase != Phase.UPDATE_EPIC:
            return UpdateEpicRequest.FULL
        context = self._update_epic_context()
        if context is not None and context.section_void and not context.selection_void:
            return UpdateEpicRequest.ROADMAP
        if (context is not None and context.selection_void) or (
            context is None and self._adopted_progress_comment_url
        ):
            if state.merged_since_epic_update > 0:
                return UpdateEpicRequest.SELECTION_WITH_ROADMAP
            return UpdateEpicRequest.SELECTION
        return UpdateEpicRequest.FULL

    def _published_progress_comment_url(self) -> str:
        """The progress comment this phase entry posted or adopted, or ``""``."""
        effects = self._require_state().phase_effects()
        for record in effects.records:
            if record.kind == EffectKind.PROGRESS_COMMENT and record.observed:
                return str(record.observed["url"])
        if effects.observation is not None:
            adopted = effects.observation.objects.get(self._progress_marker())
            if adopted:
                return adopted
        return self._adopted_progress_comment_url

    def _roadmap_section_required(self, next_issue_url: str | None) -> bool:
        """Whether this entry writes the roadmap section: a batch is due, or the
        EPIC is reported complete while merges are uncounted."""
        state = self._require_state()
        return state.merged_since_epic_update > 0 and (
            self._roadmap_update_due(state) or next_issue_url is None
        )

    def _roadmap_requirement_reason(self) -> str:
        state = self._require_state()
        pending = state.merged_since_epic_update
        if self._roadmap_update_due(state):
            return (
                f"{pending} merge(s) since the last update, workflow.epic_update_every = "
                f"{self.config.workflow.epic_update_every}"
            )
        return f"the EPIC is reported complete with {pending} merge(s) since the last update"

    def _check_update_epic_result(
        self, payload: dict, request: UpdateEpicRequest
    ) -> UpdateEpicResult:
        """The controller's half of the UPDATE_EPIC schema, before any effect.

        The parser validates each field; this adds what only the controller
        knows: whether a roadmap section is required now, and whether the
        progress comment as it would be posted (the text, a blank line, the
        marker) is publishable as a whole (D8.3). Either refusal is a
        :class:`ControlResultValidationError`, so it takes the correction
        path with nothing posted.
        """
        res = UpdateEpicResult.from_payload(payload, request)
        selection = res.next_issue_url
        if request == UpdateEpicRequest.ROADMAP:
            context = self._update_epic_context()
            selection = context.next_issue_url if context is not None else None
        if res.roadmap_section is None and self._roadmap_section_required(selection):
            raise ControlResultValidationError(
                "CONTROL_RESULT for UPDATE_EPIC missing required field 'roadmap_section' "
                f"({self._roadmap_requirement_reason()}); the controller writes it into the "
                "EPIC body between the roadmap markers. Nothing was posted."
            )
        if res.progress is not None:
            problem = published_payload_problem(
                "progress comment", progress_comment_body(res.progress, self._progress_marker())
            )
            if problem:
                raise ControlResultValidationError(problem)
        return res

    def _section_digests(self, section: str | None) -> tuple[str | None, str | None]:
        """D4.6: the outside-markers digests of the body read before the launch,
        as read and as the splice of ``section`` leaves it."""
        if section is None:
            return None, None
        entry = self._epic_roadmap_at_entry
        if entry is None:
            raise StateError("UPDATE_EPIC result applied without the entry's EPIC body read")
        spliced = split_roadmap(splice_roadmap(entry.body, section))
        return sha256_text(entry.outside), sha256_text(spliced.outside)

    def _save_update_epic_context(
        self, context: UpdateEpicContext, records: list[EffectRecord] | None = None
    ) -> None:
        """Validate and persist the context (and a new plan) in one save (D3.1)."""
        state = self._require_state()
        if records is not None:
            state.effect_records = [r.to_dict() for r in records]
        state.completion_context = context.to_dict()
        # Strictly validated exactly as a later load will validate it: a
        # value that fails is never written.
        state.phase_effects()
        self._save()

    def _apply_update_epic(self, payload: dict) -> tuple[Phase, str]:
        """Plan, publish and complete UPDATE_EPIC from the agent's result.

        A ``FULL`` result plans the progress comment (K8) and saves it with
        the completion context in one save, after a precondition read finds
        the EPIC exactly as the entry observed it (no comment carries the
        marker; anything else is unexplained and blocks, §2.9). A re-request
        result replaces only the input a rejection voided (D4.7), and a
        legacy adoption saves the context with no record (D13.7). Then
        :meth:`_complete_update_epic` does the rest from what was saved, as a
        later entry would.
        """
        state = self._require_state()
        request = self._update_epic_request()
        res = self._check_update_epic_result(payload, request)
        context = self._update_epic_context()
        if request == UpdateEpicRequest.FULL:
            return self._plan_progress_comment(res)
        if context is not None:
            return self._apply_re_request(context, res, request)
        if self._adopted_progress_comment_url:
            return self._adopt_legacy_progress(res)
        raise StateError(
            f"an UPDATE_EPIC {request.value} result arrived for issue "
            f"{state.current_issue_url} with no published progress comment to complete"
        )

    def _precondition_holder(self) -> tuple[str | None, str]:
        """Re-read the EPIC's progress comments: (the one holder's URL, block text)."""
        state = self._require_state()
        try:
            holder = self._progress_comments().at_most_one()
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            return None, (
                f"the progress comments of EPIC {state.epic_url} could not be read "
                f"unambiguously before posting: {exc}. Nothing was posted"
            )
        return (holder.obj.url if holder is not None else None), ""

    def _plan_progress_comment(self, res: UpdateEpicResult) -> tuple[Phase, str]:
        state = self._require_state()
        assert res.progress is not None
        url, problem = self._precondition_holder()
        if problem:
            return Phase.BLOCKED, problem
        if url is not None:
            return Phase.BLOCKED, self._unjournaled_progress_text(url)
        section = (
            res.roadmap_section if self._roadmap_section_required(res.next_issue_url) else None
        )
        entry_digest, spliced_digest = self._section_digests(section)
        context = UpdateEpicContext(
            issue_url=state.current_issue_url,
            pr_url=state.current_pr_url,
            roadmap_section=section,
            next_issue_url=res.next_issue_url,
            entry_outside_sha256=entry_digest,
            spliced_outside_sha256=spliced_digest,
        )
        epic = parse_issue_url(state.epic_url).canonical
        marker = self._progress_marker()
        record = EffectRecord.plan(
            0,
            EffectKind.PROGRESS_COMMENT,
            EffectOwner(
                run_id=state.run_id,
                phase=Phase.UPDATE_EPIC,
                issue_url=state.current_issue_url,
                pr_url=state.current_pr_url,
                transaction_id="",
            ),
            identity={"epic_url": epic, "marker": marker},
            target={"epic_url": epic},
            precondition={"absent": True},
            payload={"body": progress_comment_body(res.progress, marker)},
        )
        self._save_update_epic_context(context, [record])
        return self._complete_update_epic()

    def _apply_re_request(
        self, context: UpdateEpicContext, res: UpdateEpicResult, request: UpdateEpicRequest
    ) -> tuple[Phase, str]:
        """D4.7: replace the voided input(s) of the saved context, nothing else."""
        selection = context.next_issue_url
        section = context.roadmap_section
        digests = (context.entry_outside_sha256, context.spliced_outside_sha256)
        if request != UpdateEpicRequest.ROADMAP:
            selection = res.next_issue_url
        if request == UpdateEpicRequest.ROADMAP or (
            request == UpdateEpicRequest.SELECTION_WITH_ROADMAP
            and self._roadmap_section_required(selection)
        ):
            # The section is composed against the body read before this
            # launch, so its digests are taken from that read.
            section = res.roadmap_section
            digests = self._section_digests(section)
        elif request == UpdateEpicRequest.SELECTION_WITH_ROADMAP:
            section, digests = None, (None, None)
        updated = replace(
            context,
            roadmap_section=section,
            next_issue_url=selection,
            entry_outside_sha256=digests[0],
            spliced_outside_sha256=digests[1],
            selection_void=False,
            section_void=False,
        )
        self._save_update_epic_context(updated)
        return self._complete_update_epic()

    def _adopt_legacy_progress(self, res: UpdateEpicResult) -> tuple[Phase, str]:
        """D13.7: complete around the one legacy progress comment, with no record."""
        state = self._require_state()
        adopted = self._adopted_progress_comment_url
        url, problem = self._precondition_holder()
        if problem:
            return Phase.BLOCKED, problem
        if url != adopted:
            return Phase.BLOCKED, (
                f"the progress comment {adopted} adopted for issue {state.current_issue_url} "
                f"is no longer the one comment carrying its marker on EPIC {state.epic_url} "
                f"(found: {url or 'none'}). Nothing was posted; inspect the EPIC's comments, "
                "then 'unblock'"
            )
        section = (
            res.roadmap_section if self._roadmap_section_required(res.next_issue_url) else None
        )
        entry_digest, spliced_digest = self._section_digests(section)
        context = UpdateEpicContext(
            issue_url=state.current_issue_url,
            pr_url=state.current_pr_url,
            roadmap_section=section,
            next_issue_url=res.next_issue_url,
            entry_outside_sha256=entry_digest,
            spliced_outside_sha256=spliced_digest,
        )
        self._save_update_epic_context(context)
        return self._complete_update_epic()

    def _persist_effect(self, record: EffectRecord) -> None:
        """D4.1: each stage change of a record is durable before the next step."""
        state = self._require_state()
        state.effect_records[record.position] = record.to_dict()
        self._save()

    def _drive_progress_records(self) -> str:
        """Reconcile, and issue at most once, every pending record; "" or a block reason.

        A conflict names the object and stops the phase. An unavailable
        GitHub, or a write whose outcome is not readable yet
        (:class:`EffectPending`), propagates with the record left as
        persisted: the next entry reconciles it before anything is sent.
        """
        state = self._require_state()
        for record in state.phase_effects().records:
            if record.stage == Stage.CONFLICT:
                return self._effect_conflict_text(record)
            if not record.pending:
                continue
            try:
                driven = drive(record, ProgressCommentOp(self.github), self._persist_effect)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                return (
                    f"{record.describe()} could not be reconciled with GitHub: {exc}. This is "
                    "not a transient failure (authentication, permissions, or malformed data); "
                    "nothing was sent again. Fix the cause, then 'unblock'"
                )
            if driven.record.stage == Stage.CONFLICT:
                return self._effect_conflict_text(driven.record)
        return ""

    def _effect_conflict_text(self, record: EffectRecord) -> str:
        state = self._require_state()
        return (
            f"{record.reason}. The controller never repairs, duplicates or chooses between "
            f"such objects; the run stays on issue {state.current_issue_url}, nothing was "
            "switched. Inspect the EPIC, remove the object(s) named above that the "
            "controller did not post, then 'unblock': the record is reconciled again within "
            "its attempt bound"
        )

    def _complete_update_epic(self) -> tuple[Phase, str]:
        """Finish UPDATE_EPIC from the persisted context: publish, splice, select.

        The same code for the step that saved the context and for every
        later entry (journal first, §2.10): the K8 record is reconciled and
        issued at most once; the roadmap section is spliced when one is
        stored and merges are pending, and the merge counter is reset only
        after the read-back; then the selection is verified before the issue
        switches. A rejected input is voided in the context and raised for
        'resume' to re-request (D4.7); a conclusive GitHub failure blocks.
        """
        state = self._require_state()
        reason = self._drive_progress_records()
        if reason:
            return Phase.BLOCKED, reason
        context = self._update_epic_context()
        if context is None or context.void:
            raise StateError("UPDATE_EPIC completion reached without a complete context")
        published = self._published_progress_comment_url()
        if not published:
            # The load refuses a context with neither a K8 record nor an
            # adopted comment; this keeps completion closed if one ever got
            # here, before the roadmap write and the issue switch.
            raise StateError(
                f"UPDATE_EPIC completion for issue {state.current_issue_url} reached with no "
                "progress comment posted or adopted; the roadmap section was not written and "
                "the issue was not switched"
            )
        try:
            roadmap_note = self._splice_roadmap_section(context)
        except GitHubUnavailableError:
            raise
        except (GitHubError, RoadmapError) as exc:
            return Phase.BLOCKED, (
                f"the roadmap section of EPIC {state.epic_url} could not be written and read "
                f"back: {exc}. This is not a transient GitHub failure, so asking the agent "
                "again would not help; the merge counter was not reset and the run stays on "
                f"issue {state.current_issue_url}. Inspect the EPIC body, fix the cause, "
                "then 'resume'."
            )
        done = f"progress comment {published} on the EPIC; {roadmap_note}"
        selection = context.next_issue_url
        if selection is None:
            state.next_issue_rejections = []
            nxt = self._next_phase(Phase.UPDATE_EPIC, {"next_issue_url": None})
            return nxt, f"{done}; UPDATE_EPIC -> {nxt.value}"
        try:
            issue = self._verify_issue_selectable(selection, switching=True)
        except VerificationError as exc:
            self._void_selection(context)
            return self._reject_next_issue(str(exc), cause=exc)
        except GitHubUnavailableError as exc:
            self._void_selection(context)
            return self._reject_next_issue(
                f"next issue {selection} could not be verified (GitHub unavailable: {exc})",
                cause=exc,
            )
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"next issue {selection} could not be verified on GitHub: {exc}. This is not "
                "a transient GitHub failure (authentication, permissions, or malformed data), "
                "so asking the agent to select again would not help; the run stays on issue "
                f"{state.current_issue_url}, nothing was switched. Fix the cause, then "
                "'resume'."
            )
        state.reset_for_new_issue(parse_issue_url(selection).canonical)
        nxt = self._next_phase(Phase.UPDATE_EPIC, {"next_issue_url": selection})
        return nxt, (
            f"{done}; verified next issue #{issue.number} ({issue.state}); "
            f"UPDATE_EPIC -> {nxt.value}"
        )

    def _void_selection(self, context: UpdateEpicContext) -> None:
        """D4.7: the rejected selection leaves the context; the rest stays published."""
        state = self._require_state()
        voided = replace(context, next_issue_url=None, selection_void=True)
        state.completion_context = voided.to_dict()

    def _void_section(self, context: UpdateEpicContext, when: str) -> NoReturn:
        """D4.6: an outside edit voids the stored section; persist, then re-request it."""
        state = self._require_state()
        voided = replace(
            context,
            roadmap_section=None,
            entry_outside_sha256=None,
            spliced_outside_sha256=None,
            section_void=True,
        )
        state.completion_context = voided.to_dict()
        self._save()
        raise VerificationError(
            f"the body of EPIC {state.epic_url} changed outside the roadmap markers {when} "
            "(a human may edit the EPIC at any time). The controller did not write over it; "
            "the progress comment stays published and the merge counter was not reset. "
            "'resume' re-reads the body and asks the agent for the roadmap section only."
        )

    def _splice_roadmap_section(self, context: UpdateEpicContext) -> str:
        """Write the stored roadmap section into the EPIC body; reset the merge
        counter only once the write has been read back (D4.6).

        The controller owns the whole edit: it splices the section between
        the roadmap markers of the body it reads now, writes the body, reads
        it back, and requires the section to be the one written. The bytes
        outside the markers must equal one of the two digests the context
        stored: the body the agent composed against, or that body with the
        section appended. Anything else is an outside edit: the section is
        voided and re-requested, never written over the edit. A body that
        already carries the section is not written again, so a crash after
        the write costs no second body write; a read-back that shows another
        section is a VerificationError, and 'resume' splices the stored
        section again. GitHub being unavailable propagates (transient); a
        conclusive failure or unreadable markers are the caller's to block on.
        """
        state = self._require_state()
        pending = state.merged_since_epic_update
        section = context.roadmap_section
        if section is None or pending == 0:
            return (
                f"roadmap update not due ({pending} merge(s) since the last update, "
                f"workflow.epic_update_every = {self.config.workflow.epic_update_every}); "
                "EPIC body not written, merge counter kept"
            )
        why = self._roadmap_requirement_reason()
        known = {context.entry_outside_sha256, context.spliced_outside_sha256}
        before = self._read_epic_roadmap()
        if sha256_text(before.outside) not in known:
            self._void_section(context, "since the agent composed the section")
        new_body = splice_roadmap(before.body, section)
        if new_body == before.body:
            state.record_epic_update()
            return (
                f"roadmap update due ({why}); the EPIC body already carries this section "
                "(no write needed); merge counter reset"
            )
        self.github.edit_issue_body(state.epic_url, new_body)
        after = self._read_epic_roadmap()
        if sha256_text(after.outside) not in known:
            self._void_section(context, "while the section was written")
        if after.section != section:
            raise VerificationError(
                f"after writing the roadmap section, EPIC {state.epic_url} does not carry "
                "the section that was written (read back from GitHub); the merge counter "
                "was not reset — 'resume' writes the stored section again."
            )
        state.record_epic_update()
        action = "replaced" if before.section is not None else "appended"
        return (
            f"roadmap update due ({why}); {action} the managed roadmap section of the EPIC "
            "body, read back, bytes outside the markers unchanged; merge counter reset"
        )


def check_template_files() -> list[str]:
    """Return sorted names of missing prompt templates (empty == all good)."""
    missing = []
    for name in TEMPLATE_FILES:
        if not (Path(__file__).parent / "prompts" / name).exists():
            missing.append(name)
    return missing
