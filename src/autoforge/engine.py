"""ControllerEngine: deterministic orchestration over agent CLIs.

Core primitive is :meth:`step` (exactly one phase execution); :meth:`run`
loops ``step()`` until a STOP phase. Transition topology lives in
``transitions.py``; this engine applies it and, crucially, **verifies every
agent claim against GitHub** before acting on it:

- ANALYZE_EXECUTE: PR exists, belongs to this repo, is OPEN, head SHA and
  branch match what the agent reported.
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
  merge queue. Only then, because it executes the PR's code, the reviewed
  commit is exported into a private temporary directory (never the
  operator's checkout, never a worktree) and ``merge.verification_commands``
  run there; any failure -> BLOCKED. Conclusive negatives -> BLOCKED; HEAD
  or base drift -> REVIEW; inconclusive data raises and keeps the phase,
  re-checked on ``resume`` at most ``merge.max_verification_attempts``
  times, then BLOCKED. Before any of that, the clean review must be bound
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
import re
import secrets
from collections.abc import Callable, Hashable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import overload

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
    marker_json,
    render_follow_up_marker,
    render_implementation_marker,
    render_progress_marker,
)
from .config import DEFAULT_STATE_DIR, AutoForgeConfig, validate_required_profiles
from .errors import (
    ClaimConflictError,
    ConfigurationError,
    ControlResultError,
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    GitHubError,
    GitHubNotFoundError,
    GitHubUnavailableError,
    LockError,
    StateError,
    StateTransitionError,
    VerificationError,
)
from .executor import DEFAULT_MAX_OUTPUT_BYTES, ExecutionRequest, execute
from .github import (
    CommentInfo,
    GitHubClient,
    IssueInfo,
    PRInfo,
    WorkflowRunJobs,
    build_merge_argv,
)
from .local_workspace import (
    FeatureSpec,
    LocalWorkspace,
    WorkspaceSnapshot,
    read_feature_spec,
    verify_feature_spec_unchanged,
)
from .locking import ControllerLock, repository_lock_path
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
from .profiles import local_required_profiles, profile_for_phase
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
    MAX_RESOLUTIONS_PER_FIX,
    MIN_RATIONALE_CHARS,
    AnalyzeExecuteResult,
    FixResult,
    LocalAnalyzeExecuteResult,
    LocalFixResult,
    LocalReviewResult,
    ReplanReexecuteResult,
    ReviewResult,
    UpdateEpicResult,
    parse_control_result,
)
from .run_contract import LocalRunContract, validate_local_run_contract
from .runlog import ExecutionRecord, RunLogger
from .safefs import SafeRoot
from .state import (
    STATE_FILENAME,
    AutoForgeState,
    StatePaths,
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
}
# The FIX payload bounds (#77), stated to the fixer the same way.
FIX_BOUND_VARIABLES: dict[str, str | int | None] = {
    "MAX_RESOLUTIONS_PER_FIX": MAX_RESOLUTIONS_PER_FIX,
    "MAX_FIX_RATIONALE_CHARS": MAX_FIX_RATIONALE_CHARS,
    "MIN_RATIONALE_CHARS": MIN_RATIONALE_CHARS,
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


REQUIRED_PROFILES = [
    "analyze_execute",
    "fix",
    "review_round_1",
    "review_round_2_5",
    "review_round_6_plus",
    "replan_reexecute",
]

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


_REVIEW_HEADING_RE = re.compile(r"^#\s*AI Code Review\s*[—–-]+\s*Round\s+(\d+)\s*$", re.MULTILINE)


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


@dataclass
class StepPlan:
    phase: str
    profile_name: str
    provider: str
    model: str
    effort: str
    command: list[str]
    timeout_seconds: int
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
        "adopts the open PR carrying the issue's implementation marker, if one exists, "
        "instead of relaunching the agent"
    ),
    Phase.REVIEW: (
        "hands a review comment already posted for this round at this HEAD to the "
        "reviewer to adopt instead of posting a second one"
    ),
    Phase.FIX: (
        "routes a HEAD already pushed past the reviewed one back to REVIEW instead of "
        "relaunching the fixer"
    ),
    Phase.REPLAN_REEXECUTE: "replays the persisted replan transaction",
    Phase.UPDATE_EPIC: (
        "hands a progress comment already posted on the EPIC for this issue to the "
        "agent to adopt instead of posting a second one"
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
        self._workspace: LocalWorkspace | None = None
        # The workspace snapshot bound immediately before a LOCAL agent
        # phase. The prompt is rendered from it, so the fingerprint the
        # reviewer is told to report is exactly the one persisted in state.
        self._local_bound_snapshot: WorkspaceSnapshot | None = None
        # True while the LOCAL phase being invoked is a *retry* of an
        # invocation that was already launched once (see
        # ``AutoForgeState.local_pending_phase``); the prompt then tells the
        # agent that work from the earlier attempt may already be present.
        self._local_resumed_invocation = False
        # The review comment the REVIEW entry probe found already posted for
        # the upcoming round at the bound HEAD (see
        # :meth:`_reconcile_review_entry`), handed to the reviewer as
        # ``EXISTING_REVIEW_COMMENT_URL`` so it adopts it instead of posting a
        # second one. Re-derived from GitHub on every REVIEW entry.
        self._existing_review_comment_url = ""
        # Likewise for the other phases whose agents write to GitHub: the EPIC
        # progress comment the UPDATE_EPIC entry found already posted for the
        # finished issue (``EXISTING_PROGRESS_COMMENT_URL``), and the open
        # follow-up issues the FIX entry found already carrying a marker for
        # one of the open findings (rendered into ``FOLLOW_UP_ISSUES``).
        # Re-derived from GitHub on every entry, never persisted.
        self._existing_progress_comment_url = ""
        self._existing_follow_ups: dict[str, str] = {}
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

        A REMOTE run has no contract and is launched from ``workdir`` as
        before; GitHub, not a working-tree verifier, is its source of truth.
        """
        if self.mode is WorkflowMode.LOCAL:
            return self.local_contract().repository_root
        return self.workdir

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
        """Per open finding: its follow-up marker and the follow-up issue that exists.

        Controller-rendered, not agent text: the finding ids passed the
        parser's shape check (``R<n>-F<m>``), the marker is built by
        :func:`render_follow_up_marker` and the URLs come from GitHub.
        """
        if not pr_url or not findings:
            return "(none)"
        lines = []
        for f in findings:
            fid = str(f.get("id"))
            marker = render_follow_up_marker(pr_url, fid)
            url = existing.get(fid, "(none)")
            lines.append(
                f"- {escape_inline(fid)}: marker `{marker}`; existing issue: {escape_inline(url)}"
            )
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
            "EXISTING_REVIEW_COMMENT_URL": self._existing_review_comment_url or "(none)",
            "EXISTING_PROGRESS_COMMENT_URL": self._existing_progress_comment_url or "(none)",
            "PROGRESS_MARKER": (
                render_progress_marker(s.current_issue_url, s.current_pr_url)
                if s.current_issue_url and s.current_pr_url
                else "(none)"
            ),
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
            # The base the round is bound to, next to the HEAD: the reviewer
            # copies it into the marker, so it is also given as marker-safe
            # JSON (a branch name may contain a double quote, which a raw
            # substitution inside the marker's JSON would break, or a comment
            # delimiter such as `-->`, which would end the marker early).
            "REVIEWED_BASE_REF": escape_inline(reviewed_base or "(none)"),
            "REVIEWED_BASE_REF_JSON": marker_json(reviewed_base or "(none)"),
            "FINDINGS": self._format_findings(s.open_findings),
            "PRIOR_FINDINGS": self._format_prior_findings(),
            "MERGED_SINCE_EPIC_UPDATE": s.merged_since_epic_update,
            "LAST_REVIEW_RESULT": s.last_review_result or "(none)",
            "NEXT_ISSUE_REJECTION": (
                s.next_issue_rejections[-1] if s.next_issue_rejections else "(none)"
            ),
            **REVIEW_BOUND_VARIABLES,
            **FIX_BOUND_VARIABLES,
        }
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

    def template_for(self, phase: Phase) -> str | None:
        """The prompt template of ``phase`` in the loaded run's mode."""
        table = LOCAL_PHASE_TEMPLATE if self.mode == WorkflowMode.LOCAL else PHASE_TEMPLATE
        if self.mode == WorkflowMode.LOCAL and phase not in table:
            raise StateTransitionError(
                f"phase {phase.value} belongs to the REMOTE workflow and is never executed "
                "by a local run"
            )
        return table.get(phase)

    def render_prompt_for(self, phase: Phase, correction_error: str | None = None) -> str:
        local = self.mode == WorkflowMode.LOCAL
        template = self.template_for(phase)
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
                    "base, not draft, no change to "
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
                    "reviewed HEAD and base, not draft, no change to "
                    "safety.protected_merge_paths, all checks "
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
            notes.append("would first check for an existing open PR (recovery -> REVIEW)")
        if s.phase == Phase.REVIEW:
            cap = next_round_cap_reason(s.review_round, self.config.workflow.max_review_rounds)
            if cap:
                notes.append(f"would enter BLOCKED without invoking the reviewer: {cap}")
            notes.append(
                f"review round {s.review_round + 1} of at most "
                f"{self.config.workflow.max_review_rounds} (workflow.max_review_rounds)"
            )
            notes.append(
                "REVIEWED_HEAD_SHA and REVIEWED_BASE_REF are fetched from gh immediately "
                "before the review"
            )
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
        return StepPlan(
            phase=s.phase.value,
            profile_name=profile.name,
            provider=profile.provider,
            model=profile.model,
            effort=profile.effort,
            command=command,
            timeout_seconds=profile.timeout_seconds
            or self.config.execution.default_timeout_seconds,
            prompt_length=len(prompt),
            prompt_preview=prompt[:1200],
            prompt_full=prompt,
            routing=self._routing_info(s, profile.name),
            template=PHASE_TEMPLATE[s.phase] or "",
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
            timeout_seconds=profile.timeout_seconds
            or self.config.execution.default_timeout_seconds,
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
            timeout_seconds=0,
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
            Phase.ANALYZE_EXECUTE: "REVIEW (after PR verification via gh)",
            Phase.REVIEW: "FIX if any finding, READY_FOR_MERGE if clean, REVIEW if HEAD moved",
            Phase.FIX: "REVIEW (after new HEAD verification via gh)",
            Phase.REPLAN_REEXECUTE: "REVIEW (replacement PR; fresh review round 1)",
            Phase.MERGE: "UPDATE_EPIC (controller merge verified as MERGED via gh) | REVIEW",
            Phase.UPDATE_EPIC: "ANALYZE_EXECUTE | DONE",
        }.get(phase, "")

    @staticmethod
    def _legal_next_for(phase: Phase, mode: WorkflowMode = WorkflowMode.REMOTE) -> list[str]:
        return sorted(p.value for p in edges_for(mode).get(phase, frozenset()))

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
    ) -> list[StepOutcome]:
        """Loop ``step()`` until a stop phase or ``max_steps``.

        READY_FOR_MERGE is a stop phase only while the merge gate is closed.
        With the gate open (config AND ``allow_merge``) the loop continues
        through the controller-side pre-merge verification, MERGE and
        UPDATE_EPIC, so ``resume --allow-merge`` re-checks inconclusive
        GitHub data (bounded by ``merge.max_verification_attempts``).
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
                self.state.phase = Phase.ANALYZE_EXECUTE
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
                all_outcomes.append(self._step_once(dry_run=False, allow_merge=allow_merge))
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

        invoked = self._invoke_phase(previous, lambda: self._remote_entry(previous, plan))
        if isinstance(invoked, StepOutcome):
            # A correction relaunch was pre-empted by the entry reconciliation:
            # the malformed attempt's GitHub work resolved the phase.
            return invoked
        payload = invoked
        status = payload.get("status")
        if status in ("failure", "blocked"):
            nxt = Phase.FAILED if status == "failure" else Phase.BLOCKED
            # Agent-supplied text, persisted in a plain `state.json` and
            # printed by the CLI: same redaction boundary as the run log.
            message = redact(str(payload.get("message", "") or f"agent reported {status}"))
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
            return self._try_recover_pr()
        if previous == Phase.REVIEW:
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
            # printed by the CLI: same redaction boundary as the run log.
            message = redact(str(payload.get("message", "") or f"agent reported {status}"))
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
        validate_transition(Phase.INITIALIZING, Phase.ANALYZE_EXECUTE, WorkflowMode.LOCAL)
        state.phase = Phase.ANALYZE_EXECUTE
        self._save()
        return self._outcome(
            Phase.INITIALIZING,
            plan=plan,
            message=(
                f"froze feature specification {spec.relative_path} "
                f"(sha256 {spec.sha256[:16]}...) at {snapshot.anchor}; "
                "INITIALIZING -> ANALYZE_EXECUTE"
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
        return Phase.REVIEW, (
            f"implementation verified: working tree now holds {verified.describe()}, "
            f"fingerprint {verified.fingerprint[:16]}...{tests}; "
            "ANALYZE_EXECUTE -> REVIEW"
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
            return Phase.FIX, (
                f"local review round {res.round}: {len(findings)} finding(s); REVIEW -> FIX "
                f"(fix round {state.local_fix_rounds + 1} of {budget})"
            )
        state.last_review_result = "clean"
        state.open_findings = []
        return Phase.DONE, (
            f"local review round {res.round} clean for workspace {bound[:16]}...; "
            "REVIEW -> DONE. The implementation is in the working tree; committing it is "
            "yours to do."
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
        return Phase.REVIEW, (
            f"local fix round {round_no} verified: {len(res.resolutions)} "
            f"resolution(s){note}; FIX -> REVIEW (round {state.review_round + 1})"
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
            req = ExecutionRequest(
                command=list(argv),
                cwd=cwd,
                timeout_seconds=self.config.execution.default_timeout_seconds,
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
                metadata={"validation_command": list(argv), "feature": state.feature_spec_path},
            )
            if result.timed_out or result.exit_code != 0:
                record.error = (
                    f"timed out after {req.timeout_seconds}s"
                    if result.timed_out
                    else f"exit {result.exit_code}"
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
                    f"validation command {shown!r} timed out after "
                    f"{req.timeout_seconds}s; {phase.value} is not verified."
                )
            if result.exit_code != 0:
                tail = redact((result.stderr or result.stdout or "").strip())[-2000:]
                raise VerificationError(
                    f"validation command {shown!r} failed with exit "
                    f"{result.exit_code}; {phase.value} is not verified and the run stays in "
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
        validate_transition(Phase.INITIALIZING, Phase.ANALYZE_EXECUTE)
        state.phase = Phase.ANALYZE_EXECUTE
        state.current_branch = ""
        self._save()
        return self._outcome(
            Phase.INITIALIZING,
            plan=plan,
            message=(
                f"verified issue #{issue.number} ({issue.state}); INITIALIZING -> ANALYZE_EXECUTE"
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

    def _try_recover_pr(self) -> StepOutcome | None:
        """Idempotency guard before ANALYZE_EXECUTE.

        Returns a StepOutcome when the phase was resolved without invoking the
        agent (recovered -> REVIEW, or ambiguous -> BLOCKED); None otherwise.

        The issue's implementation PR is the open PR carrying the
        ``ai-implementation`` marker for it (:func:`render_implementation_marker`),
        found in a strict listing of the repository's open PRs: a listing the
        client cannot prove complete blocks, because "no PR exists" is then
        not knowable and launching an agent on that guess is how a second
        implementation gets created. The marker is the identity
        :meth:`_apply_analyze` requires of the PR the agent claims, so a PR
        the read-back would accept is a PR every later entry finds, whatever
        its branch is called and whether or not GitHub links it to the
        issue. A PR the controller already persisted is a candidate as well,
        marker or not: it is the controller's own verified record.
        """
        state = self._require_state()
        pr, problem = self._implementation_pr_candidate()
        if problem:
            return self._block(Phase.ANALYZE_EXECUTE, None, problem)
        if pr is None:
            return None
        url = parse_pr_url(pr.url).canonical
        state.current_pr_url = url
        state.current_head_sha = pr.head_sha
        state.current_base_ref = pr.base_ref
        state.current_branch = pr.head_ref
        validate_transition(Phase.ANALYZE_EXECUTE, Phase.REVIEW)
        state.phase = Phase.REVIEW
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.ANALYZE_EXECUTE,
            message=(
                f"recovered existing open PR {url} (HEAD {pr.head_sha[:12]}, "
                f"branch {pr.head_ref}); ANALYZE_EXECUTE -> REVIEW without invoking the agent"
            ),
        )

    def _implementation_pr_candidate(self) -> tuple[PRInfo | None, str]:
        """The one open PR that implements the current issue, if it is knowable.

        The probe behind :meth:`_try_recover_pr` (and behind ``unblock`` for
        a run with no PR bound): the persisted PR if it is still open, plus
        the open PR carrying the issue's ``ai-implementation`` marker from a
        strict listing. Returns ``(pr, "")`` for exactly one usable
        candidate, ``(None, "")`` for none, and ``(None, reason)`` when the
        answer is not knowable -- an unreadable persisted PR, a listing that
        cannot be proven complete, a marker defect, two candidates, a PR of
        another repository or one without a readable HEAD. ``reason`` is the
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
        if not pr.head_sha:
            return None, f"open PR {url} has no readable head SHA; cannot recover"
        return pr, ""

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
                    f"PR {canonical} is MERGED and already counted; UPDATE_EPIC will "
                    "reconcile the EPIC progress comment before launching its agent",
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
                "FIX re-reads the HEAD before launching the fixer",
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
        validate_transition(Phase.READY_FOR_MERGE, Phase.MERGE)
        state.phase = Phase.MERGE
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.READY_FOR_MERGE,
            plan=plan,
            message=(
                f"merge gate open and GitHub confirms PR {verified.url} is mergeable at the "
                f"reviewed HEAD {verified.head_sha[:12]} on {verified.base_ref!r}; "
                "READY_FOR_MERGE -> MERGE"
            ),
        )

    def merge_gate_open(self, allow_merge: bool) -> bool:
        """The merge safety gate: config ``safety.allow_merge`` AND the CLI flag."""
        return bool(self.config.merge_allowed_by_config and allow_merge)

    def _check_merge_gate(self, allow_merge: bool) -> None:
        if not self.merge_gate_open(allow_merge):
            raise VerificationError(MERGE_GATE_MESSAGE)

    def _block(self, previous: Phase, plan: StepPlan | None, reason: str) -> StepOutcome:
        """Enter BLOCKED with a human-readable reason (never guess)."""
        state = self._require_state()
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
        never closes; the budget ends the run at the next step instead.
        """
        if phase is not Phase.REPLAN_REEXECUTE:
            return True
        journal = self._require_state().replan_transaction
        return budget_may_stop(ReplanTransaction.from_dict(journal))

    def _replan_finishes_under_budget_note(self, budget: str) -> str:
        """Dry-run note for a replan step that runs although the budget is reached.

        Says only *that* the step runs and how it is charged; *what* it does
        is the stage's own note (:meth:`_replan_plan`), so the two cannot
        describe different actions.
        """
        txn = ReplanTransaction.from_dict(self._require_state().replan_transaction)
        if txn.stage in CLOSE_BEGUN_STAGES:
            return (
                f"step budget reached ({budget}), but the replan transaction has already begun "
                f"closing the source PR {txn.source_pr_url} (stage {txn.stage.value}); would "
                "finish it as described in the stage note below, without invoking an agent and "
                "without closing anything; the step counts and the budget ends the run at the "
                "next step"
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
        command only there), the close is reachable from ``VERIFIED`` alone,
        and every later stage reads GitHub and then activates, undoes or
        refuses -- which of the three is a GitHub fact the plan cannot read,
        so it names all of them. Pure: reads the journal and nothing else.
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
            return [
                f"would re-read the source PR {source} via gh; never closes it from here and "
                "invokes no agent",
                "source CLOSED and carrying this transaction's close receipt: would confirm both "
                f"checkpoints, then activate the replacement {replacement} or undo the close "
                "if a checkpoint moved",
                "source OPEN, or closed without the receipt: would refuse and enter BLOCKED "
                "naming both PRs, since local state cannot tell a close that never ran from "
                "one that landed and was reopened",
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

        - merge gate closed / no clean review bound to a PR, HEAD and base
          -> raises, nothing changes
        - ``current_pr_url`` is not the reviewed PR by identity, or GitHub
          answers the reviewed URL with a different PR -> BLOCKED before
          anything else is read from it: the review is never re-bound to
          another PR, whatever its HEAD
        - PR already MERGED: from MERGE this is crash recovery (counted once
          if it merged at the reviewed HEAD *and* base, else BLOCKED); from
          READY_FOR_MERGE -> MERGE so that phase reconciles
        - PR CLOSED, draft, conflicting, failing check, branch protection,
          auto-merge armed, merge queue -> BLOCKED (conclusive; no retry)
        - PR HEAD != reviewed HEAD, or PR base != reviewed base -> REVIEW
          (clean review is stale)
        - inconclusive (mergeability UNKNOWN, checks running, GitHub read
          failed transiently) -> raises and keeps the phase, bounded by
          ``merge.max_verification_attempts``
        - GitHub read failed conclusively (auth, permissions, unresolvable
          PR) -> BLOCKED
        """
        state = self._require_state()
        self._check_merge_gate(allow_merge)
        if not state.current_pr_url:
            raise StateError(f"phase {phase.value} requires current_pr_url in state")
        url = state.current_pr_url
        reviewed = (state.reviewed_head_sha or "").lower()
        reviewed_base = state.reviewed_base_ref
        if (
            state.last_review_result != "clean"
            or not reviewed
            or not state.reviewed_pr_url
            or not reviewed_base
        ):
            raise VerificationError(
                f"{phase.value} requires a clean review bound to a PR, a HEAD and a base "
                f"branch in state (last_review_result={state.last_review_result!r}, "
                f"reviewed_pr_url={state.reviewed_pr_url!r}, "
                f"reviewed_head_sha={state.reviewed_head_sha!r}, "
                f"reviewed_base_ref={reviewed_base!r}); refusing to merge"
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
                validate_transition(Phase.READY_FOR_MERGE, Phase.MERGE)
                state.phase = Phase.MERGE
                state.attempt = 0
                self._save()
                return self._outcome(
                    phase,
                    plan=plan,
                    message=(
                        f"PR {url} is already MERGED on GitHub; READY_FOR_MERGE -> MERGE to "
                        "reconcile (nothing counted yet)"
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
        ``execution.default_timeout_seconds``. The directory is deleted
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
                fetch_pr_head(runner, repo, pr.number)
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

    def _run_premerge_command(self, phase: Phase, pr: PRInfo, argv: list[str], cwd: str) -> str:
        """One ``merge.verification_commands`` entry in the exported tree; "" on exit 0."""
        state = self._require_state()
        req = ExecutionRequest(
            command=list(argv),
            cwd=cwd,
            timeout_seconds=self.config.execution.default_timeout_seconds,
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
            metadata={
                "verification_command": list(argv),
                "pr_url": pr.url,
                "head_sha": pr.head_sha,
            },
        )
        if result.timed_out or result.exit_code != 0:
            record.error = (
                f"timed out after {req.timeout_seconds}s"
                if result.timed_out
                else f"exit {result.exit_code}"
            )
        self._logger().log_execution(record, "", result.stdout or "", result.stderr or "")
        # The command line and its output both reach `block_reason` in plain
        # `state.json`, so both are redacted here as well as on the log path.
        shown = " ".join(redact_argv(list(argv)))
        if result.timed_out:
            return (
                f"pre-merge verification command {shown!r} timed out after "
                f"{req.timeout_seconds}s on the reviewed HEAD {pr.head_sha[:12]} of PR "
                f"{pr.url}; the green check is not corroborated locally"
            )
        if result.exit_code != 0:
            tail = redact((result.stderr or result.stdout or "").strip())[-2000:]
            return (
                f"pre-merge verification command {shown!r} failed with exit "
                f"{result.exit_code} on the reviewed HEAD {pr.head_sha[:12]} of PR {pr.url}; "
                f"the green check is not corroborated locally. Output tail: {tail}"
            )
        return ""

    def _revision_drift_to_review(
        self, phase: Phase, plan: StepPlan, pr: PRInfo, detail: str = "", message: str = ""
    ) -> StepOutcome:
        """The OPEN PR's revision (HEAD or base) is no longer the reviewed one.

        The last review is stale: persists the PR's current HEAD and base,
        marks the review stale and routes back to REVIEW (``phase ->
        REVIEW``), where the next round is bound to the actual revision. The
        review's open findings, if any (the FIX entry case), stop being open
        -- no fixer resolves findings of a commit that is no longer the PR --
        and become the findings that review is told to re-check
        (``prior_findings``), so an unrecorded push never makes a finding
        disappear unexamined. Nothing has been merged or counted.
        ``message`` replaces the default merge-path wording.
        """
        state = self._require_state()
        if pr.base_ref and pr.base_ref != state.reviewed_base_ref:
            what = f"PR base changed to {pr.base_ref!r}"
        else:
            what = "PR HEAD moved"
        state.current_head_sha = pr.head_sha
        if pr.base_ref:
            state.current_base_ref = pr.base_ref
        state.last_review_result = "stale"
        state.prior_findings = state.open_findings
        state.open_findings = []
        carried = (
            f"; the {len(state.prior_findings)} finding(s) of round {state.review_round} are "
            "carried to that review to re-check"
            if state.prior_findings
            else ""
        )
        validate_transition(phase, Phase.REVIEW)
        state.phase = Phase.REVIEW
        state.attempt = 0
        self._save()
        return self._outcome(
            phase,
            plan=plan,
            message=(
                message
                or (f"{what} after the clean review{detail}; {phase.value} -> REVIEW (not merged)")
            )
            + carried,
        )

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
        2. state carries a clean review bound to a PR, a HEAD and a base
        3. ``current_pr_url`` is that PR by identity, and so is the PR GitHub
           returns for it (else BLOCKED; the review is never re-bound)
        4. PR belongs to this repository; already MERGED -> crash recovery
        5. PR is OPEN, its HEAD equals the reviewed HEAD and its base is the
           reviewed base (else -> REVIEW)
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
        merge itself did not happen), nothing unreviewed was merged and the
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
            if drifted and not pending:
                # Post-verification race: the revision moved between the
                # controller's verification and the write (a push, which
                # `--match-head-commit <reviewed>` refused, or a retarget).
                # Nothing unreviewed merged; the clean review is stale ->
                # REVIEW (same rule as pre-write drift).
                return self._revision_drift_to_review(
                    Phase.MERGE,
                    plan,
                    after,
                    detail=(
                        f" (HEAD {after.head_sha[:12]} on {after.base_ref!r} != reviewed "
                        f"{reviewed[:12]} on {reviewed_base!r}; {reason}{note})"
                    ),
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
        nxt = self._after_merge_phase()
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

    @staticmethod
    def _after_merge_phase() -> Phase:
        """Deterministic post-merge routing owned by the controller.

        Always UPDATE_EPIC for now: the UPDATE_EPIC agent posts progress and
        selects the next issue (or null -> DONE). Batching several merges per
        EPIC update (MERGE -> ANALYZE_EXECUTE) is tracked separately (#13).
        """
        return Phase.UPDATE_EPIC

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

    def _bind_review_head(self) -> None:
        """Fetch the real PR HEAD and base right before the review and persist them.

        The review is a decision about the PR's diff, and the diff is the
        HEAD against the base, so both are bound here and both are re-read
        after the review (:meth:`_apply_review`): a round whose HEAD or base
        moved while the reviewer worked is stale, not current. A PR whose
        base cannot be read cannot be bound and is refused before anyone is
        launched.
        """
        state = self._require_state()
        pr = self._require_open_pr()
        if not pr.base_ref:
            raise VerificationError(
                f"PR {state.current_pr_url} has no readable base branch; the review cannot "
                "be bound to the diff it would decide on"
            )
        state.current_head_sha = pr.head_sha
        state.current_base_ref = pr.base_ref
        state.current_branch = pr.head_ref or state.current_branch
        self._save()

    def _reconcile_review_entry(self, plan: StepPlan) -> StepOutcome | None:
        """Read the PR for this round's comment before the reviewer is launched.

        A reviewer whose result was never recorded (timeout, non-zero exit,
        refused run-log write, verification failure, crash) may already have
        posted the round's comment. GitHub is the source of truth, so the
        controller looks before relaunching: exactly one comment carrying the
        ``ai-review-result`` marker for the upcoming round at the bound HEAD
        and base is handed to the reviewer (``EXISTING_REVIEW_COMMENT_URL``)
        to adopt rather than duplicate, and :meth:`_verify_review_comment`
        enforces afterwards that the round still has exactly one. Two or more
        is a state the controller cannot resolve without guessing which
        review is the round's, so it blocks without invoking anyone. Comments
        for the same round at another HEAD or against another base (an
        earlier run, a stale re-review, a round posted before the PR was
        retargeted) are not this round's and are ignored: a review of the
        diff against the old base is not a review of the diff against the
        new one, and adopting it would record the new base as reviewed.

        The reviewer is also told which problems earlier rounds already
        deferred: the open issues carrying this PR's ``ai-follow-up`` marker
        for any finding id (``EXISTING_FOLLOW_UP_ISSUES``). Finding ids are
        round-scoped, so a problem re-raised under a new id would otherwise
        be deferred again into a second issue by the next fixer; the review
        that knows about the first issue does not re-raise it (#90). The
        listing is strict for the same reason the FIX entry's is: "no such
        issue exists" is not knowable from a listing that may be truncated.
        """
        state = self._require_state()
        self._existing_review_comment_url = ""
        self._existing_pr_follow_ups = []
        upcoming = state.review_round + 1
        head = state.current_head_sha.lower()
        base = state.current_base_ref
        pr_ref = parse_pr_url(state.current_pr_url)
        try:
            holder = self._review_comments(pr_ref, upcoming, head, base).at_most_one()
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.REVIEW,
                plan,
                f"cannot establish which comment carries the review for round {upcoming} at "
                f"HEAD {head[:12]} on base {base!r} of PR {pr_ref.canonical}: {exc}. The "
                "controller will not launch a reviewer that could post a second review comment",
            )
        except ClaimConflictError as exc:
            return self._block(
                Phase.REVIEW,
                plan,
                f"{exc}. The controller never chooses between them: remove or edit the extra "
                "or unreadable comment(s) so exactly one remains, then start a new run",
            )
        if holder is not None:
            self._existing_review_comment_url = holder.obj.url
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
        return None

    def _prepare_fix(self, plan: StepPlan) -> StepOutcome | None:
        """Re-read the PR before the fixer runs; an unreviewed push goes back to REVIEW.

        FIX resolves the findings of one review, and those findings are bound
        to the HEAD that review saw. A PR HEAD past it before the fixer is
        launched is a push the controller never verified: an earlier fixer
        whose result was not recorded (timeout, non-zero exit, refused run-log
        write, verification failure, crash) or an operator. Which findings
        that push resolved, if any, is not knowable from controller state and
        is never inferred, so the rule for every other HEAD drift applies
        here too: the review is stale and the actual HEAD is reviewed
        (``FIX -> REVIEW``) instead of a fixer being launched against findings
        of a commit that is no longer the PR. The cost is one review round;
        the review of the actual HEAD is what says what remains.

        A push is not the only write a fixer makes: a ``follow_up_created``
        resolution creates an issue and moves no HEAD. So with the HEAD
        still the reviewed one, the open issues of the repository are read
        for the ``ai-follow-up`` marker of (this PR, an open finding id):
        exactly one per finding is handed to the fixer (``FOLLOW_UP_ISSUES``)
        to report instead of creating a second, two or more for one finding
        is a state the controller cannot resolve without choosing and blocks
        without launching anyone, and a listing that cannot be proven
        complete blocks too, because "no such issue exists" is then not
        knowable. :meth:`_apply_fix` enforces the same one-per-finding rule
        on read-back.
        """
        state = self._require_state()
        self._existing_follow_ups = {}
        self._existing_pr_follow_ups = []
        if not state.open_findings:
            raise StateError("FIX phase entered without open findings in state")
        pr = self._require_open_pr()
        reviewed = state.reviewed_head_sha.lower()
        if pr.head_sha.lower() != reviewed:
            state.last_fix_resolutions = []
            return self._revision_drift_to_review(
                Phase.FIX,
                plan,
                pr,
                message=(
                    f"PR HEAD {pr.head_sha[:12]} is past the reviewed HEAD {reviewed[:12]} "
                    f"that the open findings of round {state.review_round} are bound to "
                    "(an unrecorded fix or an operator push; the controller does not infer "
                    "which findings it resolved); FIX -> REVIEW of the actual HEAD, no fixer "
                    "launched"
                ),
            )
        pr_ref = parse_pr_url(state.current_pr_url)
        open_ids = [str(f["id"]) for f in state.open_findings]
        try:
            # One listing serves both: the open findings' own follow-ups
            # (checked below) and the earlier rounds' deferrals, handed to
            # the fixer so a re-raised problem is recorded on the issue that
            # already exists instead of in a second one (#90).
            follow_ups = self._follow_up_issues(pr_ref)
            for fid in open_ids:
                holder = follow_ups.claimants(
                    (pr_ref.identity, fid), _finding_what(pr_ref, fid)
                ).at_most_one()
                if holder is not None:
                    self._existing_follow_ups[fid] = holder.obj.url
            deferred = follow_ups.grouped(
                lambda c: c.finding_id not in open_ids, f"PR {pr_ref.canonical}"
            )
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._block(
                Phase.FIX,
                plan,
                f"cannot establish which follow-up issues already exist for the open "
                f"findings of PR {pr_ref.canonical}: {exc}. The controller will not launch a "
                "fixer that could create a second one",
            )
        except ClaimConflictError as exc:
            return self._block(
                Phase.FIX,
                plan,
                f"{exc}. The controller never chooses between them: close or repair the "
                "extra or unreadable issue(s) so exactly one remains, then start a new run",
            )
        self._existing_pr_follow_ups = _follow_up_pairs(deferred)
        state.current_head_sha = pr.head_sha
        self._save()
        return None

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
        """The open PRs claiming to implement ``issue``, from one strict listing."""
        state = self._require_state()
        prs = self.github.list_open_prs(state.repository, strict=True)
        return collect(IMPLEMENTATION, prs, "open PR").claimants(
            issue.identity, f"issue {issue.canonical}"
        )

    def _review_comments(
        self, pr_ref: GitHubPullRequestRef, round_: int, head: str, base: str
    ) -> Claimants[CommentInfo, ReviewClaim]:
        """The PR's comments claiming review ``round_`` of ``head`` against ``base``.

        The key is the revision the round decided on, HEAD *and* base: a
        comment carrying the round's marker for the same HEAD against
        another base (posted before the PR was retargeted), or for no base
        at all (written before the marker recorded one), is not this
        round's and matches nothing, so it is neither adopted at entry nor
        accepted on read-back.
        """
        if not base:
            raise StateError(
                f"review round {round_} of PR {pr_ref.canonical} has no bound base branch"
            )
        comments = self.github.get_pr_comments(pr_ref.canonical)
        return collect(REVIEW, comments, "comment").claimants(
            (round_, head.lower(), base),
            f"round {round_} at HEAD {head[:12]} on base {base!r} of PR {pr_ref.canonical}",
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
        """Read the EPIC for this issue's progress comment before the agent runs.

        UPDATE_EPIC's writes are a progress comment on the EPIC and its task
        list edits; the comment is the one with an identity, and it carries
        the ``ai-epic-progress`` marker of (finished issue, merged PR). An
        agent whose result was never recorded may already have posted it, and
        so may the agent of a rejected selection that is being asked again.
        Exactly one such comment is handed to the agent
        (``EXISTING_PROGRESS_COMMENT_URL``) to adopt rather than duplicate;
        two or more block without launching anyone; :meth:`_apply_update_epic`
        enforces afterwards that the EPIC carries exactly one. The task list
        edits are idempotent by nature (a checked box stays checked) and are
        not read back; their confinement is #4 and #13.
        """
        state = self._require_state()
        self._existing_progress_comment_url = ""
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
                "not launch an agent that could post a second one",
            )
        except ClaimConflictError as exc:
            return self._block(
                Phase.UPDATE_EPIC,
                plan,
                f"{exc}. The controller never chooses between them: remove or repair the "
                "extra or unreadable comment(s) so exactly one remains, then start a new run",
            )
        if holder is not None:
            self._existing_progress_comment_url = holder.obj.url
        return None

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
            # Repository-wide and strict, for the same reason the candidate
            # lookup is: an issue-shaped filter cannot see a PR that is not
            # (yet) linked to the issue.
            preexisting = self.github.list_open_prs(state.repository, strict=True)
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
        drift = verify_decision_point(source, txn)
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
            # Every open PR in the repository, strictly: "no candidate exists"
            # decides whether the agent runs again, so it must be neither a
            # truncated listing in disguise nor a filtered one. A replacement
            # the agent created but had not yet linked to the issue is exactly
            # what an issue-shaped filter drops, and re-running the agent on it
            # is the one outcome crash idempotency forbids.
            open_prs = self.github.list_open_prs(state.repository, strict=True)
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
        must never count as proof. A resume never posts a receipt and never
        performs the close: the write is reachable from ``VERIFIED`` only. A
        CLOSED source without the receipt is refused, and so is an OPEN one
        under a recorded intent -- with or without the receipt -- because the
        journal cannot tell a close that never ran from one that landed, lost
        its receipt to a crash, and was then reopened by a human (R11-F1).
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
            # An OPEN source under a durable intent is never closed from here.
            # The intent proves a close was *about* to be attempted, not
            # whether it was: "crashed before `gh pr close` ran" and "closed,
            # crashed before the receipt was published, then reopened by a
            # human" leave the same OPEN source with no receipt, and only the
            # second is a decision a retry would override (R11-F1). The
            # receipt can make the second story certain; its absence never
            # makes the first one so. Both refuse, naming the story the
            # evidence supports, and the write below stays reachable from
            # VERIFIED alone.
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
            return self._reject_replan(
                txn,
                f"source PR {txn.source_pr_url} is open under a recorded close intent for replan "
                f"transaction {txn.transaction_id} but carries no close receipt; the close may "
                "never have run, or it may have landed and been reopened by a human before the "
                "receipt was published, and local state cannot tell the two apart, so the "
                "controller will not close it",
            )
        # Reached from VERIFIED only: the destructive write is ahead, and no
        # earlier attempt at it was ever recorded. Revalidate both sides now.
        try:
            target = self.github.get_pr(txn.replacement_pr_url)
            open_prs = self.github.list_open_prs(state.repository, strict=True)
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
        drift = verify_source_checkpoint(source, txn)
        if drift:
            return self._reject_replan(txn, drift)
        txn.stage = ReplanStage.SUPERSEDE_INTENT
        txn.close_intent_at = utcnow_iso()
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
        # only by the step that observed its own close; a resume never posts.
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
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            drift = (
                f"source PR {txn.source_pr_url} could not be re-read after the close "
                f"({exc}), so the close cannot be confirmed against its checkpoint"
            )
        else:
            drift = verify_closed_source(source, txn)
        if not drift:
            try:
                target = self.github.get_pr(txn.replacement_pr_url)
                open_prs = self.github.list_open_prs(state.repository, strict=True)
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
            target = self.github.get_pr(txn.replacement_pr_url)
            open_prs = self.github.list_open_prs(state.repository, strict=True)
        except GitHubUnavailableError:
            raise  # unknown, not refused: `resume` re-reads and re-verifies
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"the source PR was closed, but the replan checkpoints could not be re-read "
                f"before activating the replacement ({exc})",
                txn.replacement_pr_url,
            )
        drift = verify_closed_source(source, txn) or self._target_drift(target, open_prs, txn)
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
        state.reviewed_pr_url = ""
        state.reviewed_head_sha = ""
        state.reviewed_base_ref = ""
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
        validate_transition(Phase.REPLAN_REEXECUTE, Phase.REVIEW)
        state.phase = Phase.REVIEW
        state.attempt = 0
        self._save()
        return self._outcome(
            Phase.REPLAN_REEXECUTE,
            message=(
                f"replacement PR {txn.replacement_pr_url} verified (replan transaction "
                f"{txn.transaction_id}); source PR {txn.source_pr_url} closed without merge; "
                "REPLAN_REEXECUTE -> REVIEW (fresh round 1)"
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
        timeout = profile.timeout_seconds or self.config.execution.default_timeout_seconds
        max_corrections = max(0, self.config.execution.max_correction_attempts)
        # Read once per invocation, before the loop: the agent and the record
        # of it are launched from the same directory, and for a LOCAL run
        # that directory comes from the contract (see :meth:`_execution_cwd`).
        cwd = self._execution_cwd()
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
            # The one write before the agent starts, in both modes: the
            # attempt counter and, for a LOCAL write phase, the checkpoint
            # charged just above land together. A crash while the agent runs,
            # or a refused run-log write after it returned, therefore never
            # leaves controller state claiming the phase was not yet
            # attempted (#55, PR #89).
            self._save()
            req = AgentRequest(
                phase=phase.value,
                prompt=prompt,
                cwd=cwd,
                profile=profile,
                timeout_seconds=timeout,
                attempt=state.attempt,
                correction=correction_error is not None,
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
                timeout_seconds=timeout,
                metadata=self._log_metadata(phase),
            )
            result: AgentExecutionResult | None = None
            try:
                result = provider.execute(req)
            except ExecutionError as exc:
                record.error = f"{type(exc).__name__}: {exc}"
                self._record_invocation(logger, record, prompt, "", "", phase)
                raise
            record.started_at = result.started_at
            record.finished_at = result.finished_at
            record.exit_code = result.exit_code
            record.timed_out = result.timed_out
            record.stdout_truncated = result.stdout_truncated
            record.stderr_truncated = result.stderr_truncated
            stdout, stderr = result.stdout or "", result.stderr or ""
            if result.timed_out:
                record.error = f"timed out after {timeout}s"
                self._record_invocation(logger, record, prompt, stdout, stderr, phase)
                raise ExecutionTimeoutError(
                    f"agent '{profile.name}' timed out after {timeout}s and was killed. "
                    "State unchanged — inspect the real Git/GitHub state, then 'resume'."
                )
            if result.exit_code != 0:
                record.error = f"exit {result.exit_code}"
                self._record_invocation(logger, record, prompt, stdout, stderr, phase)
                raise ExecutionError(
                    f"agent '{profile.name}' exited {result.exit_code}. "
                    f"stderr tail: {stderr[-2000:]} "
                    "State unchanged — inspect logs, then 'resume'."
                )
            try:
                # Only the tail of a truncated stdout is searched: the block
                # is the last thing the agent writes, so the kept tail holds
                # a whole one; a block before the cut is stale or spans it.
                payload = parse_control_result(result.stdout_tail, phase, state.mode)
            except (ControlResultError, ControlResultValidationError) as exc:
                detail = str(exc)
                if result.stdout_truncated:
                    detail += (
                        f" (stdout exceeded the {DEFAULT_MAX_OUTPUT_BYTES}-byte capture bound; "
                        "only the last part of it was searched -- keep the transcript short "
                        "and end it with the CONTROL_RESULT block)"
                    )
                record.error = f"{type(exc).__name__}: {detail}"
                self._record_invocation(logger, record, prompt, stdout, stderr, phase)
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
            self._record_invocation(logger, record, prompt, stdout, stderr, phase)
            return payload

    def _record_invocation(
        self,
        logger: RunLogger,
        record: ExecutionRecord,
        prompt: str,
        stdout: str,
        stderr: str,
        phase: Phase,
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
            logger.log_execution(record, prompt, stdout, stderr)
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
            return self._apply_update_epic(UpdateEpicResult.from_payload(payload))
        raise StateTransitionError(f"phase {phase.value} does not accept agent results")

    def _apply_analyze(self, res: AnalyzeExecuteResult) -> tuple[Phase, str]:
        state = self._require_state()
        issue = parse_issue_url(res.issue_url)
        if not same_issue_url(issue.canonical, state.current_issue_url):
            raise VerificationError(
                f"agent reported issue {issue.canonical} but the run is for "
                f"{state.current_issue_url}"
            )
        pr_ref = parse_pr_url(res.pr_url)
        if pr_ref.repository.lower() != state.repository.lower():
            raise VerificationError(
                f"agent reported PR {pr_ref.canonical} outside repository {state.repository}"
            )
        # The identity the next entry will look for, read the way the entry
        # reads it: one strict listing of the open PRs, in which exactly one
        # carries this issue's implementation marker, and it is the PR the
        # agent reported. Accepting a PR without the marker would persist a
        # PR no re-entry after a lost state file could find again; accepting
        # one of two would choose, and the entry never chooses. The reported
        # PR's state, HEAD and branch are read from the same snapshot.
        marker = render_implementation_marker(state.current_issue_url)
        try:
            holder = self._implementation_prs(issue).exactly_one()
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            raise VerificationError(
                f"cannot accept PR {pr_ref.canonical} as the implementation of issue "
                f"#{issue.number}: {exc}; the marker {marker!r} identifies the issue's one "
                "open PR"
            ) from exc
        pr = holder.obj
        if not parse_pr_url(pr.url).same_target(pr_ref):
            raise VerificationError(
                f"agent reported PR {pr_ref.canonical} but the open PR carrying the "
                f"implementation marker {marker!r} is {pr.url}"
            )
        if not pr.is_open:
            raise VerificationError(f"PR {pr_ref.canonical} is {pr.state}, expected OPEN")
        if pr.head_sha != res.head_sha:
            raise VerificationError(
                f"PR head mismatch: GitHub reports {pr.head_sha} for {pr_ref.canonical}, "
                f"agent claimed {res.head_sha}; refusing to enter REVIEW"
            )
        if pr.head_ref and pr.head_ref != res.branch:
            raise VerificationError(
                f"PR branch mismatch: GitHub reports {pr.head_ref!r}, agent claimed {res.branch!r}"
            )
        state.current_pr_url = pr_ref.canonical
        state.current_head_sha = pr.head_sha
        state.current_base_ref = pr.base_ref
        state.current_branch = pr.head_ref or res.branch
        state.review_round = 0
        state.open_findings = []
        state.prior_findings = []
        state.review_history = []
        state.last_review_result = ""
        state.reviewed_pr_url = ""
        state.reviewed_head_sha = ""
        state.reviewed_base_ref = ""
        return Phase.REVIEW, (
            f"PR {pr_ref.canonical} verified (HEAD {pr.head_sha[:12]}, branch "
            f"{state.current_branch}); ANALYZE_EXECUTE -> REVIEW"
        )

    def _verify_review_comment(
        self, res: ReviewResult, expected_head: str, expected_base: str
    ) -> CommentInfo:
        """Verify the review comment the result names and return it as GitHub read it.

        The returned :class:`CommentInfo` is the comment the controller
        located on the PR (the one carrying the round's marker at the bound
        HEAD and base), matched to the result's ``review_comment_url`` by
        identity. Its ``url`` is GitHub's URL of that comment, which is what
        the round persists and hands to the fixer: the reviewer's spelling of
        the URL (``Owner/REPO`` for ``owner/repo``, or the ``issues/<n>`` path
        GitHub also serves a PR comment under) is accepted as naming the same
        comment but is never the handoff artifact.
        """
        state = self._require_state()
        try:
            cref = parse_comment_url(res.review_comment_url)
        except Exception as exc:
            raise VerificationError(
                f"review_comment_url is not a GitHub PR comment URL: {exc}"
            ) from exc
        pr_ref = parse_pr_url(state.current_pr_url)
        if not cref.on(pr_ref):
            raise VerificationError(
                f"review comment {res.review_comment_url} does not belong to PR {pr_ref.canonical}"
            )
        # The same read the entry made: exactly one comment on the PR claims
        # this round at this HEAD against this base, and it is the comment
        # the result names. A second one, whoever posted it, leaves the
        # round's review ambiguous and the next REVIEW entry would block on
        # it rather than choose; a comment whose marker is unreadable is
        # refused for the same reason it would be refused at entry; and a
        # comment for this round and HEAD against another base (or none) is
        # a review of a different diff that the reviewer was told not to
        # adopt, so a result naming it is refused too.
        try:
            holder = self._review_comments(
                pr_ref, res.round, expected_head, expected_base
            ).exactly_one()
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            raise VerificationError(
                f"{exc}; a round has exactly one review comment at its HEAD and base"
            ) from exc
        if not parse_comment_url(holder.obj.url).same_target(cref):
            raise VerificationError(
                f"review comment {res.review_comment_url} is not the comment carrying the "
                f"round {res.round} marker at HEAD {expected_head[:12]} on base "
                f"{expected_base!r} (that is {holder.obj.url})"
            )
        heading = _REVIEW_HEADING_RE.search(holder.obj.body or "")
        if heading is None or int(heading.group(1)) != res.round:
            raise VerificationError(
                f"review comment lacks the '# AI Code Review — Round {res.round}' heading"
            )
        if holder.claim.needs_fix_round != res.needs_fix_round:
            raise VerificationError(
                "review comment marker needs_fix_round disagrees with CONTROL_RESULT"
            )
        # The marker's finding ids are the durable copy of the round's
        # findings; when published they must be the findings being
        # persisted, as a set (order is presentation, and both sides are
        # distinct by construction). Both lists hold validated ids, so
        # quoting them is safe.
        if holder.claim.finding_ids is not None:
            marked = sorted(holder.claim.finding_ids)
            reported = sorted(f.id for f in res.findings)
            if marked != reported:
                raise VerificationError(
                    f"review comment marker finding_ids {marked} disagree with the "
                    f"CONTROL_RESULT findings {reported}"
                )
        return holder.obj

    def _apply_review(self, res: ReviewResult) -> tuple[Phase, str]:
        state = self._require_state()
        expected_round = state.review_round + 1
        expected_head = state.current_head_sha.lower()
        if res.round != expected_round:
            raise VerificationError(
                f"REVIEW round mismatch: expected {expected_round}, got {res.round}"
            )
        if res.reviewed_head_sha != expected_head:
            raise VerificationError(
                f"REVIEW SHA mismatch: controller bound HEAD {expected_head}, "
                f"agent reviewed {res.reviewed_head_sha}"
            )
        expected_base = state.current_base_ref
        if not expected_base:
            raise VerificationError(
                f"REVIEW round {res.round} was launched without a bound base branch; the "
                "review cannot be recorded against the diff it decided on"
            )
        verified = self._verify_review_comment(res, expected_head, expected_base)
        # The handoff artifact is GitHub's URL of the comment the controller
        # verified, never the reviewer's spelling of it (#80): the FIX prompt,
        # `state.json` and the review history all name the comment as the
        # GitHub object that was read. The parsed canonical form is the
        # fallback for a client that reported no URL for the comment.
        verified_comment_url = verified.url or parse_comment_url(res.review_comment_url).canonical

        # Valid review lifecycle: round is consumed regardless of the outcome.
        # The round is bound to the revision it decided on -- the PR the
        # comment was verified to belong to, the HEAD and the base -- and the
        # merge gate later requires all three, not the HEAD alone.
        state.review_round = res.round
        state.reviewed_pr_url = parse_pr_url(state.current_pr_url).canonical
        state.reviewed_head_sha = expected_head
        state.reviewed_base_ref = expected_base
        state.last_review_comment_url = verified_comment_url
        state.last_review_needs_fix = res.needs_fix_round
        # Findings are agent-authored text persisted in plain `state.json` and
        # rendered into the next FIX prompt, so they cross the same redaction
        # boundary as the run log.
        findings = [redact_dict(f.to_dict()) for f in res.findings]

        latest = self._require_open_pr()
        moved = ""
        if latest.head_sha != expected_head:
            moved = f"PR HEAD moved to {latest.head_sha[:12]}"
        elif latest.base_ref != expected_base:
            moved = f"PR base changed from {expected_base!r} to {latest.base_ref!r}"
        if moved:
            state.current_head_sha = latest.head_sha
            state.current_base_ref = latest.base_ref
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
            self._record_review(res.round, expected_head, RESULT_STALE, findings)
            carried = (
                f"; its {len(findings)} finding(s) are carried to that review to re-check"
                if findings
                else ""
            )
            return Phase.REVIEW, (
                f"review round {res.round} completed for {expected_head[:12]} but {moved} "
                f"during the review; re-reviewing the latest revision{carried}"
            )
        # A completed round of the actual revision decides about the carried
        # findings: the reviewer was shown them and re-raised, under this
        # round's ids, the ones that still apply.
        state.prior_findings = []
        if res.needs_fix_round:
            state.last_review_result = "needs_fix"
            state.open_findings = findings
            self._record_review(res.round, expected_head, RESULT_NEEDS_FIX, findings)
            workflow_stagnation = stagnation_reason(
                state.review_history,
                self.config.workflow.stagnation_identical_rounds,
                self.config.workflow.stagnation_unchanged_count_rounds,
            )
            decision = evaluate_replan_policy(
                has_actionable_findings=True,
                current_review_round=res.round,
                review_history=state.review_history,
                escalation_count=state.escalation_count,
                config=self.config.review.replan,
                workflow_stagnation_reason=workflow_stagnation,
            )
            if decision.action == "block_for_human":
                return Phase.BLOCKED, self._loop_block_reason(
                    self._replan_refusal(decision) + ". Human intervention is required"
                )
            if decision.action == "replan":
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
                        f"review round {res.round}: {len(findings)} finding(s); controller "
                        f"policy triggered REPLAN_REEXECUTE ({decision.reason}), but {unbound}, "
                        "so the replan decision cannot bind the revision it was made on. Human "
                        "intervention is required"
                    )
                # Only the decision is recorded here, together with the issue,
                # the PR, the revision it was made on and the base that
                # revision was reviewed against (bound at REVIEW entry and
                # verified non-empty above). The checkpoint and the
                # transaction id are created by `_prepare_replan`, inside the
                # REPLAN_REEXECUTE step that owns them.
                state.replan_transaction = ReplanTransaction(
                    stage=ReplanStage.PENDING,
                    issue_url=decision_issue,
                    decision_pr_url=decision_pr,
                    decision_head_sha=expected_head,
                    decision_branch=state.current_branch,
                    decision_base_ref=expected_base,
                    escalation=decision.metadata or {"trigger": decision.reason},
                ).to_dict()
                return Phase.REPLAN_REEXECUTE, (
                    f"review round {res.round}: {len(findings)} finding(s); controller policy "
                    f"triggered REPLAN_REEXECUTE ({decision.reason})"
                )
            stop = self._review_loop_stop_reason(res.round)
            if stop:
                # Findings stay persisted for the human; no FIX is started.
                return Phase.BLOCKED, self._loop_block_reason(
                    f"review round {res.round}: {len(findings)} finding(s), but {stop}"
                )
            return Phase.FIX, (
                f"review round {res.round}: {len(findings)} finding(s); REVIEW -> FIX"
            )
        state.last_review_result = "clean"
        state.open_findings = []
        self._record_review(res.round, expected_head, RESULT_CLEAN, findings)
        return Phase.READY_FOR_MERGE, (
            f"review round {res.round} clean for HEAD {expected_head[:12]}; "
            "REVIEW -> READY_FOR_MERGE"
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
        state = self._require_state()
        expected_prev = state.current_head_sha.lower()
        if res.previous_head_sha != expected_prev:
            raise VerificationError(
                f"FIX previous_head_sha {res.previous_head_sha} != controller HEAD {expected_prev}"
            )
        open_ids = [f["id"] for f in state.open_findings]
        reported = [r.finding_id for r in res.resolutions]
        missing = sorted(set(open_ids) - set(reported))
        extra = sorted(set(reported) - set(open_ids))
        if missing or extra:
            raise VerificationError(
                f"FIX resolutions must cover exactly the open findings; missing={missing} "
                f"unknown={extra}"
            )
        pr_ref = parse_pr_url(state.current_pr_url)
        pr_url = pr_ref.canonical
        # The same read the entry made: the open issues carrying this PR's
        # follow-up marker, per finding. A follow-up the result claims must
        # be the one marked open issue of its finding, and a finding resolved
        # any other way must have none: the marked issue is the durable
        # record of the decision, and state never records a different one.
        follow_ups = self._follow_up_issues(pr_ref)
        for r in res.resolutions:
            try:
                marked = follow_ups.claimants(
                    (pr_ref.identity, r.finding_id), _finding_what(pr_ref, r.finding_id)
                ).at_most_one()
            except ClaimConflictError as exc:
                raise VerificationError(
                    f"{exc}; a finding has at most one follow-up issue"
                ) from exc
            if r.resolution != "follow_up_created":
                if marked is not None:
                    raise VerificationError(
                        f"{r.finding_id} is resolved as {r.resolution} but open issue "
                        f"{marked.obj.url} carries its follow-up marker"
                    )
                continue
            ref = parse_issue_url(r.follow_up_issue_url)
            if ref.repository.lower() != state.repository.lower():
                raise VerificationError(
                    f"follow-up issue {ref.canonical} for {r.finding_id} is outside "
                    f"{state.repository}"
                )
            if same_issue_url(ref.canonical, state.current_issue_url):
                raise VerificationError(
                    f"follow-up for {r.finding_id} points at the current issue itself"
                )
            try:
                issue = self.github.get_issue(ref.canonical)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                raise VerificationError(
                    f"follow-up issue {ref.canonical} for {r.finding_id} does not exist: {exc}"
                ) from exc
            if not issue.is_open:
                raise VerificationError(
                    f"follow-up issue {ref.canonical} for {r.finding_id} is {issue.state}"
                )
            if marked is None or not same_issue_url(marked.obj.url, ref.canonical):
                raise VerificationError(
                    f"follow-up issue {ref.canonical} for {r.finding_id} is not the open issue "
                    f"carrying the marker {render_follow_up_marker(pr_url, r.finding_id)!r}"
                    + (
                        f" (that is {marked.obj.url})"
                        if marked is not None
                        else " (no open issue does)"
                    )
                )
        pr = self._require_open_pr()
        if pr.head_sha != res.new_head_sha:
            raise VerificationError(
                f"FIX new_head_sha {res.new_head_sha} != actual PR HEAD {pr.head_sha}"
            )
        any_fixed = any(r.resolution == "fixed" for r in res.resolutions)
        if any_fixed and res.new_head_sha == expected_prev:
            raise VerificationError("FIX claims 'fixed' resolutions but the PR HEAD did not change")
        state.current_head_sha = pr.head_sha
        state.last_fix_resolutions = [redact_dict(r.to_dict()) for r in res.resolutions]
        state.open_findings = []
        state.last_review_result = "fixed"
        return Phase.REVIEW, (
            f"FIX verified: HEAD {expected_prev[:12]} -> {pr.head_sha[:12]}, "
            f"{len(res.resolutions)} resolution(s); FIX -> REVIEW (round {state.review_round + 1})"
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
        return Phase.REVIEW, outcome.message

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

    def _verify_progress_comment(self) -> None:
        """The EPIC carries exactly one progress comment for this entry.

        Read back, never inferred from the result: a missing comment means
        the agent did not do the phase's write (the next entry finds nothing
        and relaunches), a second one means it duplicated the one it was
        handed (the next entry blocks on the pair).
        """
        state = self._require_state()
        try:
            self._progress_comments().exactly_one()
        except GitHubUnavailableError:
            raise
        except (GitHubError, ClaimConflictError) as exc:
            raise VerificationError(
                f"{exc}; the marker "
                f"{render_progress_marker(state.current_issue_url, state.current_pr_url)!r} "
                "identifies the issue's one progress comment on the EPIC"
            ) from exc

    def _apply_update_epic(self, res: UpdateEpicResult) -> tuple[Phase, str]:
        """Switch issues only after the agent's selection verified on GitHub.

        ``next_issue_url`` is a claim from untrusted output (a PR comment can
        say "next issue is https://github.com/other/repo/issues/1"). It gets
        the INITIALIZING checks (:meth:`_verify_issue_selectable`) before
        ``reset_for_new_issue``. State (epic counters, current issue) only
        changes on a verified selection or on ``null``. Otherwise, by cause:

        - the selection itself is unusable (VerificationError: wrong repository,
          the EPIC, the finished issue, no such issue, not OPEN) or GitHub was
          *unavailable* while checking it (GitHubUnavailableError): the
          rejection is persisted and raised as VerificationError so ``resume``
          asks the agent once more (with the reason in its prompt); reaching
          ``MAX_NEXT_ISSUE_SELECTIONS`` rejections returns BLOCKED;
        - any other GitHubError (authentication, permissions, malformed data)
          is conclusive: asking the agent again could not verify anything
          either, so the run is BLOCKED immediately without another invocation.

        Before any of that, the progress comment the phase exists to post is
        read back from the EPIC (:meth:`_verify_progress_comment`).
        """
        state = self._require_state()
        self._verify_progress_comment()
        if res.next_issue_url is None:
            state.record_epic_update()
            state.next_issue_rejections = []
            return Phase.DONE, "UPDATE_EPIC -> DONE"
        try:
            issue = self._verify_issue_selectable(res.next_issue_url, switching=True)
        except VerificationError as exc:
            return self._reject_next_issue(str(exc), cause=exc)
        except GitHubUnavailableError as exc:
            return self._reject_next_issue(
                f"next issue {res.next_issue_url} could not be verified "
                f"(GitHub unavailable: {exc})",
                cause=exc,
            )
        except GitHubError as exc:
            return Phase.BLOCKED, (
                f"next issue {res.next_issue_url} could not be verified on GitHub: {exc}. "
                "This is not a transient GitHub failure (authentication, permissions, or "
                "malformed data), so asking the agent to select again would not help; the "
                f"run stays on issue {state.current_issue_url}, nothing was switched or "
                "counted. Fix the cause, then 'resume'."
            )
        state.record_epic_update()
        state.reset_for_new_issue(parse_issue_url(res.next_issue_url).canonical)
        return Phase.ANALYZE_EXECUTE, (
            f"verified next issue #{issue.number} ({issue.state}); UPDATE_EPIC -> ANALYZE_EXECUTE"
        )


def check_template_files() -> list[str]:
    """Return sorted names of missing prompt templates (empty == all good)."""
    missing = []
    for name in TEMPLATE_FILES:
        if not (Path(__file__).parent / "prompts" / name).exists():
            missing.append(name)
    return missing
