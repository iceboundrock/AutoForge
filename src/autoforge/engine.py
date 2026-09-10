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
  every check succeeded, ``mergeable`` is MERGEABLE, ``mergeStateStatus``
  is CLEAN/HAS_HOOKS, no auto-merge is armed and the base branch has no
  merge queue. Conclusive negatives -> BLOCKED; HEAD drift -> REVIEW;
  inconclusive data raises and keeps the phase, re-checked on ``resume``
  at most ``merge.max_verification_attempts`` times, then BLOCKED. MERGE
  then runs ``gh pr merge`` bound to the reviewed HEAD
  (``--match-head-commit``) and counts the merge only after GitHub reports
  the PR as ``MERGED``. If that call left auto-merge armed, the controller
  disarms it. No prompt is rendered and no provider is invoked.
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
  are identical (``stagnation_identical_rounds``) or whose finding count
  never changed (``stagnation_unchanged_count_rounds``) -> BLOCKED.
- ``max_total_steps``: the run's cumulative ``step_count`` (all issues, all
  phases, across ``resume``) -> BLOCKED before another step executes.
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
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __prompt_version__
from .config import AutoForgeConfig, validate_required_profiles
from .errors import (
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
from .github import GitHubClient, IssueInfo, PRInfo, build_merge_argv
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
from .profiles import profile_for_phase
from .prompts import TEMPLATE_FILES, load_template, render, render_phase
from .providers import AgentExecutionResult, AgentRequest, ProviderRegistry
from .replan import HistoricalReviewCollector, ReplanDecision, evaluate_replan_policy
from .replan_txn import (
    Disposition,
    ReplanStage,
    ReplanTransaction,
    has_close_receipt,
    new_transaction_id,
    render_close_receipt,
    select_bound_candidate,
    verify_attestation,
    verify_closed_source,
    verify_decision_point,
    verify_source_checkpoint,
    verify_target_marker,
    verify_target_pr,
)
from .result_parser import (
    AnalyzeExecuteResult,
    FixResult,
    ReplanReexecuteResult,
    ReviewResult,
    UpdateEpicResult,
    parse_control_result,
)
from .runlog import ExecutionRecord, RunLogger
from .state import AutoForgeState, StatePaths, load_state, save_state, utcnow_iso
from .transitions import (
    LEGAL_EDGES,
    STOP_PHASES,
    TERMINAL_PHASES,
    Phase,
    validate_transition,
)
from .validation import (
    parse_comment_url,
    parse_issue_url,
    parse_pr_url,
    validate_epic_and_issue,
)

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

_REVIEW_MARKER_RE = re.compile(r"<!--\s*ai-review-result:\s*(\{.*?\})\s*-->", re.DOTALL)
_REVIEW_HEADING_RE = re.compile(r"^#\s*AI Code Review\s*[—–-]+\s*Round\s+(\d+)\s*$", re.MULTILINE)


def generate_run_id() -> str:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"af-{stamp}-{secrets.token_hex(3)}"


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
        self.paths = StatePaths.from_state_dir(state_dir or config.state_dir or ".autoforge")
        self.workdir = str(workdir)
        self.providers = providers or ProviderRegistry(runner=runner)
        self.github = github or GitHubClient(
            gh_command=config.github.command,
            timeout_seconds=config.github.timeout_seconds,
        )
        self.state: AutoForgeState | None = None
        # Set by locked(): the controller lock held for a whole command.
        self._lock: ControllerLock | None = None
        # Resolved by lock_path() on first use (never for a dry run): the lock
        # is keyed by the repository that contains workdir, not by state_dir.
        self._lock_path: Path | None = None
        # True while self.state is a snapshot of state.json taken by load();
        # such a snapshot is re-read under a self-acquired execution lock.
        self._state_from_disk = False

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

    def load(self) -> AutoForgeState:
        self.state = load_state(self.paths.state_file)
        self._state_from_disk = True
        return self.state

    def _require_state(self) -> AutoForgeState:
        if self.state is None:
            raise StateError(
                "no state loaded — call load() or new_run() first "
                "('resume' never creates a run silently)"
            )
        return self.state

    def _save(self) -> None:
        assert self.state is not None
        save_state(self.state, self.paths.state_file)

    def _logger(self) -> RunLogger:
        state = self._require_state()
        return RunLogger(self.paths.logs_dir, state.run_id)

    def validate_config(self) -> None:
        """Fail early (ConfigurationError) when a required profile is unusable."""
        validate_required_profiles(self.config, REQUIRED_PROFILES)

    # -- prompt context ---------------------------------------------------
    @staticmethod
    def branch_name_for(issue_url: str) -> str:
        return f"autoforge/{parse_issue_url(issue_url).number}"

    @staticmethod
    def _format_findings(findings: list[dict]) -> str:
        if not findings:
            return "(none)"
        lines = []
        for f in findings:
            title = f.get("title") or ""
            loc = f.get("location") or ""
            head = f"- **{f.get('id')}** [{f.get('classification')}]"
            if loc:
                head += f" `{loc}`"
            if title:
                head += f" — {title}"
            lines.append(head)
            lines.append(f"  Required resolution: {f.get('required_resolution', '')}")
        return "\n".join(lines)

    def _prompt_variables(self) -> dict[str, str | int | None]:
        s = self._require_state()
        upcoming_round = s.review_round + 1
        issue_number = parse_issue_url(s.current_issue_url).number if s.current_issue_url else 0
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
            "REVIEWED_HEAD_SHA": (
                s.current_head_sha if s.phase == Phase.REVIEW else s.reviewed_head_sha
            )
            or "(none)",
            "FINDINGS": self._format_findings(s.open_findings),
            "MERGED_SINCE_EPIC_UPDATE": s.merged_since_epic_update,
            "LAST_REVIEW_RESULT": s.last_review_result or "(none)",
            "NEXT_ISSUE_REJECTION": (
                s.next_issue_rejections[-1] if s.next_issue_rejections else "(none)"
            ),
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

    def render_prompt_for(self, phase: Phase, correction_error: str | None = None) -> str:
        template = PHASE_TEMPLATE.get(phase)
        if template is None:
            raise StateTransitionError(f"phase {phase.value} has no agent prompt")
        if template not in TEMPLATE_FILES:
            raise StateTransitionError(f"unknown template {template}")
        variables = self._prompt_variables()
        prompt = render_phase(template, variables)
        if correction_error is not None:
            correction = render(
                load_template("correction.md"),
                {"PREVIOUS_ERROR": correction_error[:2000]},
            )
            prompt = prompt + "\n\n---\n\n" + correction
        return prompt

    # -- planning (pure, no side effects) ----------------------------------
    def plan_step(self) -> StepPlan:
        s = self._require_state()
        if s.phase in TERMINAL_PHASES:
            raise StateTransitionError(f"phase {s.phase.value} is terminal — nothing to execute")
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
                    "review clean, PR open at the reviewed HEAD, not draft, all checks "
                    "succeeded, mergeable, no auto-merge / merge queue (fail closed)",
                ],
            )
        if s.phase == Phase.MERGE:
            return self._deterministic_plan(
                s,
                "MERGE (controller runs `gh pr merge` itself; no agent, no prompt)",
                "UPDATE_EPIC (only after gh reports the PR as MERGED)",
                notes=[
                    MERGE_GATE_MESSAGE,
                    "would verify: last review clean, PR open at the reviewed HEAD, not "
                    "draft, all checks succeeded, mergeable, no auto-merge / merge queue",
                    "would run the command below (bound to the reviewed HEAD via "
                    "--match-head-commit), then re-read the PR and require state MERGED",
                ],
                command=self._merge_plan_command(),
            )
        profile = profile_for_phase(self.config, s.phase, s.review_round)
        variables = self._prompt_variables()
        prompt = self.render_prompt_for(s.phase)
        command = self.providers.get(profile).build_command_for(profile, prompt)
        notes: list[str] = []
        budget = step_budget_reason(s.step_count, self.config.workflow.max_total_steps)
        if budget:
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
            notes.append("REVIEWED_HEAD_SHA is fetched from gh immediately before the review")
        if s.phase == Phase.REPLAN_REEXECUTE:
            notes.append(
                "would create and verify a new replacement PR from the verified default branch"
            )
            notes.append(
                "would close the old PR without merging it only after replacement verification"
            )
            notes.append(
                "would close the old PR only after the replacement carries this transaction's "
                "marker and both checkpoints still hold"
            )
            replan_txn = ReplanTransaction.from_dict(s.replan_transaction)
            if replan_txn.escalation:
                notes.append(
                    f"replan policy: {json.dumps(replan_txn.escalation, sort_keys=True)}"
                )
            notes.append(f"replan transaction stage: {replan_txn.stage.value}")
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
            expected_next=self._expected_next(s.phase),
            legal_next=self._legal_next_for(s.phase),
            notes=notes,
        )

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
            legal_next=ControllerEngine._legal_next_for(state.phase),
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
    def _legal_next_for(phase: Phase) -> list[str]:
        return sorted(p.value for p in LEGAL_EDGES.get(phase, frozenset()))

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

        # Cumulative step budget: measured on persisted state, so `resume`
        # continues the same budget. Checked before anything executes.
        budget = step_budget_reason(state.step_count, self.config.workflow.max_total_steps)
        if budget:
            return self._block(previous, plan, self._budget_block_reason(budget))
        if previous == Phase.REVIEW:
            # Review-round cap, whatever path led here (FIX, stale re-review,
            # HEAD drift from READY_FOR_MERGE/MERGE, resume): round cap+1 never
            # starts, no HEAD is bound and no reviewer is invoked.
            cap = next_round_cap_reason(state.review_round, self.config.workflow.max_review_rounds)
            if cap:
                return self._block(previous, plan, self._loop_block_reason(cap))

        state.step_count += 1
        if previous == Phase.INITIALIZING:
            return self._initialize(plan)
        if previous == Phase.READY_FOR_MERGE:
            return self._ready_for_merge_step(plan, allow_merge)
        if previous == Phase.MERGE:
            return self._merge_step(plan, allow_merge)
        if previous == Phase.ANALYZE_EXECUTE:
            recovered = self._try_recover_pr()
            if recovered is not None:
                return recovered
        if previous == Phase.REVIEW:
            self._bind_review_head()
        if previous == Phase.FIX:
            self._prepare_fix()
        if previous == Phase.REPLAN_REEXECUTE:
            # One reducer for the fresh step and for `resume`: it either
            # resolves the transaction (activated or refused) or falls through
            # to invoke the replacement agent.
            driven = self._drive_replan()
            if driven is not None:
                return driven

        payload = self._invoke_phase(previous)
        status = payload.get("status")
        if status in ("failure", "blocked"):
            nxt = Phase.FAILED if status == "failure" else Phase.BLOCKED
            state.phase = nxt
            state.block_reason = str(payload.get("message", "") or f"agent reported {status}")
            self._save()
            return self._outcome(
                previous,
                plan=plan,
                result=payload,
                message=f"agent reported {status}: {payload.get('message', '')}",
            )

        try:
            nxt_phase, message = self._verify_and_apply(previous, payload)
        except (VerificationError, ControlResultValidationError) as exc:
            # The agent ran (and may have changed GitHub) but its claims did not
            # verify. Persist the attempt so `resume` re-enters this phase from
            # real state instead of pretending nothing happened.
            if isinstance(exc, VerificationError) and previous in (Phase.REVIEW, Phase.FIX):
                state.verification_failures.append(f"{previous.value}: {exc}")
                state.verification_failures = state.verification_failures[-20:]
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
        """
        state = self._require_state()
        issue = parse_issue_url(state.current_issue_url)
        candidates: dict[str, PRInfo] = {}
        if state.current_pr_url:
            try:
                pr = self.github.get_pr(state.current_pr_url)
            except GitHubError as exc:
                state.phase = Phase.BLOCKED
                state.block_reason = (
                    f"state references PR {state.current_pr_url} but it cannot be read: {exc}"
                )
                self._save()
                return self._outcome(Phase.ANALYZE_EXECUTE, message=state.block_reason)
            if pr.is_open:
                candidates[parse_pr_url(pr.url).canonical] = pr
        for pr in self.github.find_open_prs_for_issue(issue):
            candidates.setdefault(parse_pr_url(pr.url).canonical, pr)
        if not candidates:
            return None
        if len(candidates) > 1:
            state.phase = Phase.BLOCKED
            state.block_reason = (
                "multiple open PRs appear to belong to issue "
                f"#{issue.number}: {', '.join(sorted(candidates))}. "
                "Close the stale ones and resume; the controller never guesses."
            )
            self._save()
            return self._outcome(Phase.ANALYZE_EXECUTE, message=state.block_reason)
        url, pr = next(iter(candidates.items()))
        if parse_pr_url(url).repository.lower() != state.repository.lower():
            state.phase = Phase.BLOCKED
            state.block_reason = f"open PR {url} is not in repository {state.repository}"
            self._save()
            return self._outcome(Phase.ANALYZE_EXECUTE, message=state.block_reason)
        if not pr.head_sha:
            state.phase = Phase.BLOCKED
            state.block_reason = f"open PR {url} has no readable head SHA; cannot recover"
            self._save()
            return self._outcome(Phase.ANALYZE_EXECUTE, message=state.block_reason)
        state.current_pr_url = url
        state.current_head_sha = pr.head_sha
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
                f"reviewed HEAD {verified.head_sha[:12]}; READY_FOR_MERGE -> MERGE"
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

        - merge gate closed / no clean review bound to a HEAD -> raises,
          nothing changes
        - PR already MERGED: from MERGE this is crash recovery (counted once
          if it merged at the reviewed HEAD, else BLOCKED); from
          READY_FOR_MERGE -> MERGE so that phase reconciles
        - PR CLOSED, draft, conflicting, failing check, branch protection,
          auto-merge armed, merge queue -> BLOCKED (conclusive; no retry)
        - PR HEAD != reviewed HEAD -> REVIEW (clean review is stale)
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
        if state.last_review_result != "clean" or not reviewed:
            raise VerificationError(
                f"{phase.value} requires a clean review bound to a HEAD in state "
                f"(last_review_result={state.last_review_result!r}, "
                f"reviewed_head_sha={state.reviewed_head_sha!r}); refusing to merge"
            )
        try:
            pr = self.github.get_pr(url)
        except GitHubError as exc:
            return self._github_read_failed(phase, plan, f"PR {url} could not be read", exc)
        if parse_pr_url(pr.url or url).repository.lower() != state.repository.lower():
            raise VerificationError(f"PR {url} is not in {state.repository}")
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
            if pr.head_sha != reviewed:
                return self._block(
                    phase,
                    plan,
                    f"PR {url} is already MERGED at HEAD {pr.head_sha} but the last clean "
                    f"review covered {reviewed}; the controller never reviewed the merged "
                    "code. Inspect manually.",
                )
            return self._complete_merge(pr, plan, recovered=True)
        if not pr.is_open:
            return self._block(
                phase, plan, f"PR {url} is {pr.state}; only an OPEN PR can be merged"
            )
        if not pr.head_sha:
            raise VerificationError(f"PR {url} has no readable head SHA")
        if pr.head_sha != reviewed:
            return self._head_drift_to_review(phase, plan, pr)
        try:
            not_ready = self._merge_readiness_problem(pr)
        except VerificationError as exc:
            return self._inconclusive(phase, plan, str(exc), cause=exc)
        except GitHubError as exc:
            return self._github_read_failed(
                phase, plan, f"merge-queue status of PR {url} could not be read", exc
            )
        if not_ready:
            return self._block(phase, plan, f"{not_ready}. Nothing was merged or counted.")
        return pr

    def _head_drift_to_review(
        self, phase: Phase, plan: StepPlan, pr: PRInfo, detail: str = ""
    ) -> StepOutcome:
        """The OPEN PR's HEAD is no longer the reviewed one: the clean review is stale.

        Persists the new HEAD, marks the review stale and routes back to
        REVIEW (``phase -> REVIEW``). Nothing has been merged or counted.
        """
        state = self._require_state()
        state.current_head_sha = pr.head_sha
        state.last_review_result = "stale"
        state.open_findings = []
        validate_transition(phase, Phase.REVIEW)
        state.phase = Phase.REVIEW
        state.attempt = 0
        self._save()
        return self._outcome(
            phase,
            plan=plan,
            message=(
                f"PR HEAD moved after the clean review{detail}; {phase.value} -> REVIEW "
                "(not merged)"
            ),
        )

    # -- MERGE: controller-owned, no agent ------------------------------------------
    def _merge_step(self, plan: StepPlan, allow_merge: bool) -> StepOutcome:
        """Merge the current PR with ``gh pr merge`` — the controller, never an agent.

        Order of checks (all before any write):
        1. merge gate (config AND CLI flag)
        2. state carries a clean review bound to a HEAD
        3. PR belongs to this repository; already MERGED -> crash recovery
        4. PR is OPEN and its HEAD equals the reviewed HEAD (else -> REVIEW)
        5. GitHub says the PR is mergeable *now*: not a draft, ``mergeable``
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
        Steps 1-5 are :meth:`_verify_pr_for_merge`, shared with READY_FOR_MERGE.
        Then ``gh pr merge --<method> --match-head-commit <reviewed>``; the PR
        is re-read and the merge is counted only when GitHub says MERGED.
        If the re-read itself fails the outcome is *uncertain*: the run stays
        in MERGE (not BLOCKED, until the same bound is reached) and ``resume``
        reconciles from real GitHub state (already MERGED -> recovered and
        counted once; still OPEN -> re-verified and re-attempted). If the
        re-read finds the PR still OPEN at a *different* HEAD (pushed between
        the verification and the write; ``--match-head-commit`` refused it),
        nothing unreviewed was merged and the HEAD-drift rule applies:
        MERGE -> REVIEW, unless that call left an asynchronous merge pending
        (auto-merge / merge queue) that could still land the new HEAD, in
        which case BLOCKED. Any other conclusive non-merge is BLOCKED; merge
        failures are never blindly retried.
        """
        state = self._require_state()
        verified = self._verify_pr_for_merge(Phase.MERGE, plan, allow_merge)
        if isinstance(verified, StepOutcome):
            return verified
        url = state.current_pr_url
        reviewed = (state.reviewed_head_sha or "").lower()

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
            if after.is_open and after.head_sha and after.head_sha != reviewed and not pending:
                # Post-verification race: the HEAD moved between the controller's
                # verification and the write, so `--match-head-commit <reviewed>`
                # refused it. Nothing unreviewed merged; the clean review is
                # stale -> REVIEW (same rule as pre-write HEAD drift).
                return self._head_drift_to_review(
                    Phase.MERGE,
                    plan,
                    after,
                    detail=(
                        f" (HEAD {after.head_sha[:12]} != reviewed {reviewed[:12]}; {reason}{note})"
                    ),
                )
            return self._block(
                Phase.MERGE,
                plan,
                f"{reason}{note}. Nothing was counted; resolve on GitHub manually.",
            )
        if after.head_sha != reviewed:
            return self._block(
                Phase.MERGE,
                plan,
                f"PR {url} is MERGED at HEAD {after.head_sha}, not the reviewed HEAD "
                f"{reviewed}; refusing to count an unreviewed merge. Inspect manually.",
            )
        return self._complete_merge(after, plan, recovered=False)

    def _merge_readiness_problem(self, pr: PRInfo) -> str:
        """Controller-side pre-merge verification against GitHub (fail closed).

        Returns a non-empty reason when the PR must NOT be merged (-> BLOCKED).
        Raises VerificationError when GitHub's data is inconclusive (the
        caller keeps the phase and bounds the re-checks) and lets GitHubError
        from the merge-queue read propagate (the caller classifies it:
        transient -> same bounded path, conclusive -> BLOCKED). Returns "" only when
        every fact the controller can read says a synchronous merge of this
        exact HEAD is acceptable right now.
        """
        url = pr.url
        if pr.is_draft:
            return f"PR {url} is a draft"

        # 1. checks: all of them, not only the ones GitHub marks required
        #    (gh does not expose which are required; stricter is safer).
        failed = [c.name or "?" for c in pr.checks if c.outcome in ("failure", "unknown")]
        pending = [c.name or "?" for c in pr.checks if c.outcome == "pending"]
        if failed:
            return f"PR {url} has failing or inconclusive checks: {', '.join(failed)}"
        if pending:
            raise VerificationError(f"PR {url} has checks still running: {', '.join(pending)}")

        # 2. mergeability as computed by GitHub
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

        # 3. asynchronous merge paths the controller would not own
        if pr.auto_merge_enabled:
            return (
                f"PR {url} already has GitHub auto-merge armed; the controller only performs "
                "synchronous merges of the reviewed HEAD. Disable auto-merge on GitHub"
            )
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
        """Fetch the real PR HEAD right before the review and persist it."""
        state = self._require_state()
        pr = self._require_open_pr()
        state.current_head_sha = pr.head_sha
        state.current_branch = pr.head_ref or state.current_branch
        self._save()

    def _prepare_fix(self) -> None:
        state = self._require_state()
        if not state.open_findings:
            raise StateError("FIX phase entered without open findings in state")
        pr = self._require_open_pr()
        state.current_head_sha = pr.head_sha
        self._save()

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
        began_closing = (
            ReplanStage.SUPERSEDE_INTENT,
            ReplanStage.COMPENSATING,
            ReplanStage.SUPERSEDED,
        )
        if txn.stage in began_closing or txn.superseded_at:
            tail = (
                f"This transaction had already begun closing the source PR {txn.source_pr_url}, "
                f"and the replacement {txn.replacement_pr_url or '(none)'} was not activated"
            )
        else:
            tail = (
                f"PR {txn.source_pr_url or '(none)'} stays open with its findings; "
                "nothing was closed or merged"
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
        issue = parse_issue_url(state.current_issue_url)
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
        source_ref = parse_pr_url(source.url or state.current_pr_url)
        if source_ref.repository.lower() != state.repository.lower():
            return self._reject_replan(
                txn, f"PR {source_ref.canonical} is not in {state.repository}"
            )
        if not source.is_open or not source.head_sha:
            return self._reject_replan(
                txn,
                f"PR {source_ref.canonical} is {source.state or '(unknown)'} with HEAD "
                f"{source.head_sha or '(unreadable)'}; only an OPEN PR at a readable HEAD can be "
                "superseded",
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
        txn.transaction_id = new_transaction_id()
        txn.stage = ReplanStage.PREPARED
        txn.issue_url = issue.canonical
        txn.source_pr_url = source_ref.canonical
        txn.source_branch = state.current_branch or source.head_ref
        txn.source_head_sha = source.head_sha
        txn.source_review_round = state.review_round
        txn.base_branch = repo.default_branch
        txn.evidence_finding_count = history.recorded_finding_count
        txn.rendered_findings = history.render_findings()
        txn.rendered_observations = history.render_observations()
        txn.rendered_verification_failures = history.render_verification_failures()
        txn.preexisting_pr_urls = sorted(
            {parse_pr_url(pr.url).canonical for pr in preexisting if pr.url}
        )
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
        if claimed_url and claimed_url != canonical:
            return self._reject_replan(
                txn,
                f"the agent reports replacement PR {claimed_url}, but the PR bound to replan "
                f"transaction {txn.transaction_id} is {canonical}",
                claimed_url,
            )
        drift = verify_target_pr(
            pr, txn, state.repository, require_checkpoint_head=False
        ) or verify_attestation(attestation, txn)
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

        ``SUPERSEDE_INTENT`` is persisted before ``close_pr`` is called, so a
        crash anywhere in the write window resumes into disposition rather than
        into a second attempt. It records an *intent*, though, not a performed
        write, so it cannot by itself tell the controller's close from a human's
        in that window: the close comment carries a receipt for exactly that
        (:meth:`_close_not_ours`), and adopting someone else's close is refused.
        """
        state = self._require_state()
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
            return self._confirm_supersede(txn, source)
        # The destructive write is still ahead: revalidate both sides now.
        try:
            target = self.github.get_pr(txn.replacement_pr_url)
        except GitHubUnavailableError:
            raise
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"replacement PR {txn.replacement_pr_url} could not be re-read: {exc}",
                txn.replacement_pr_url,
            )
        drift = verify_target_pr(
            target, txn, state.repository, require_checkpoint_head=True
        ) or verify_target_marker(target, txn)
        if drift:
            return self._reject_replan(txn, drift, txn.replacement_pr_url)
        drift = verify_source_checkpoint(source, txn)
        if drift:
            return self._reject_replan(txn, drift)
        txn.stage = ReplanStage.SUPERSEDE_INTENT
        txn.close_intent_at = utcnow_iso()
        self._save_replan_txn(txn)
        close_error: GitHubError | None = None
        try:
            self.github.close_pr(
                txn.source_pr_url,
                f"Superseded by {txn.replacement_pr_url} after controller-detected review/fix "
                "non-convergence. This PR was closed without merge; the replacement starts from "
                f"the {txn.base_branch} branch. Replan transaction {txn.transaction_id}.\n\n"
                # The receipt: posted by the same `gh pr close` invocation, so
                # its presence on the closed source is what proves the close
                # was this transaction's and not a human's.
                + render_close_receipt(txn.transaction_id),
            )
        except GitHubUnavailableError:
            # The close may or may not have landed. SUPERSEDE_INTENT is already
            # persisted, so `resume` reads GitHub and resolves it either way.
            raise
        except GitHubError as exc:
            close_error = exc
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
        if source.state == "CLOSED":
            return self._confirm_supersede(txn, source)
        if close_error is not None:
            return self._reject_replan(
                txn, f"closing source PR {txn.source_pr_url} failed: {close_error}"
            )
        return self._reject_replan(
            txn,
            f"source PR {txn.source_pr_url} is {source.state or '(unknown)'} after the close "
            "attempt; expected CLOSED",
        )

    def _confirm_supersede(self, txn: ReplanTransaction, source: PRInfo) -> StepOutcome:
        """The swap half of the compare-and-swap, completed after the close.

        Reached with the source CLOSED and the intent durable -- either
        straight from the write, or on a ``resume`` that missed it. Ownership
        is established first, then both checkpoints are compared once more
        against GitHub; if either moved, the close landed on facts that had
        already changed and is *undone* (:meth:`_compensate_close`) instead of
        being activated. Only an unmoved pair reaches ``SUPERSEDED``.

        A transient GitHub failure here propagates untouched: the transaction
        stays at ``SUPERSEDE_INTENT``, and a resume runs exactly this method
        again on the same source PR.
        """
        state = self._require_state()
        unowned = self._close_not_ours(txn)
        if unowned:
            return self._reject_replan(txn, unowned)
        drift = verify_closed_source(source, txn)
        if not drift:
            try:
                target = self.github.get_pr(txn.replacement_pr_url)
            except GitHubUnavailableError:
                raise
            except GitHubError as exc:
                drift = (
                    f"replacement PR {txn.replacement_pr_url} could not be re-read after the "
                    f"close ({exc}), so the replacement cannot be confirmed"
                )
            else:
                drift = verify_target_pr(
                    target, txn, state.repository, require_checkpoint_head=True
                ) or verify_target_marker(target, txn)
        if not drift:
            return self._record_supersede(txn)
        return self._compensate_close(txn, drift)

    def _close_not_ours(self, txn: ReplanTransaction) -> str:
        """Why this transaction may not claim the source's close, or ``""``.

        ``SUPERSEDE_INTENT`` is written before ``gh pr close`` and therefore
        records only an *intended* write. A crash in that window, followed by
        a human closing the source, would otherwise be indistinguishable from
        the controller's own close -- and the resume would supersede on the
        strength of somebody else's action. ``close_pr`` publishes
        :func:`render_close_receipt` in the comment it posts as it closes, so
        the receipt is the durable evidence that the close happened *and* that
        it was this transaction's.

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
            f"transaction {txn.transaction_id}, so this transaction did not close it; refusing "
            "to supersede on a close the controller cannot prove it performed"
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
        correct when it happened, and the source is legitimately closed. The
        run blocks and says so, naming both PRs. Ownership of that close is
        already durable in ``SUPERSEDED`` and is not re-litigated.
        """
        state = self._require_state()
        try:
            source = self.github.get_pr(txn.source_pr_url)
            target = self.github.get_pr(txn.replacement_pr_url)
        except GitHubUnavailableError:
            raise  # unknown, not refused: `resume` re-reads and re-verifies
        except GitHubError as exc:
            return self._reject_replan(
                txn,
                f"the source PR was closed, but the replan checkpoints could not be re-read "
                f"before activating the replacement ({exc})",
                txn.replacement_pr_url,
            )
        drift = (
            verify_closed_source(source, txn)
            or verify_target_pr(target, txn, state.repository, require_checkpoint_head=True)
            or verify_target_marker(target, txn)
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
        """Make the verified replacement the current implementation (idempotent)."""
        state = self._require_state()
        if not any(item.get("pr_url") == txn.source_pr_url for item in state.superseded_prs):
            state.superseded_prs.append(
                {
                    "pr_url": txn.source_pr_url,
                    "branch": txn.source_branch,
                    "head_sha": txn.source_head_sha,
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
        state.reviewed_head_sha = ""
        # review_round counts completed rounds; 0 means the next replacement
        # review is the required fresh round 1.
        state.review_round = 0
        state.review_history = []
        state.open_findings = []
        state.last_fix_resolutions = []
        state.last_review_comment_url = ""
        state.last_review_result = ""
        state.last_review_needs_fix = None
        state.execution_attempt += 1
        state.escalation_count += 1
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
    def _invoke_phase(self, phase: Phase) -> dict:
        state = self._require_state()
        profile = profile_for_phase(self.config, phase, state.review_round)
        provider = self.providers.get(profile)
        provider.validate_profile(profile)
        timeout = profile.timeout_seconds or self.config.execution.default_timeout_seconds
        max_corrections = max(0, self.config.execution.max_correction_attempts)
        correction_error: str | None = None
        attempt = 0
        while True:
            attempt += 1
            state.attempt += 1
            prompt = self.render_prompt_for(phase, correction_error)
            req = AgentRequest(
                phase=phase.value,
                prompt=prompt,
                cwd=self.workdir,
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
                cwd=self.workdir,
                timeout_seconds=timeout,
                metadata=(
                    self._replan_log_metadata() if phase == Phase.REPLAN_REEXECUTE else {}
                ),
            )
            result: AgentExecutionResult | None = None
            try:
                result = provider.execute(req)
            except ExecutionError as exc:
                record.error = f"{type(exc).__name__}: {exc}"
                self._logger().log_execution(record, prompt, "", "")
                self._save()
                raise
            record.started_at = result.started_at
            record.finished_at = result.finished_at
            record.exit_code = result.exit_code
            record.timed_out = result.timed_out
            stdout, stderr = result.stdout or "", result.stderr or ""
            if result.timed_out:
                record.error = f"timed out after {timeout}s"
                self._logger().log_execution(record, prompt, stdout, stderr)
                self._save()
                raise ExecutionTimeoutError(
                    f"agent '{profile.name}' timed out after {timeout}s and was killed. "
                    "State unchanged — inspect the real Git/GitHub state, then 'resume'."
                )
            if result.exit_code != 0:
                record.error = f"exit {result.exit_code}"
                self._logger().log_execution(record, prompt, stdout, stderr)
                self._save()
                raise ExecutionError(
                    f"agent '{profile.name}' exited {result.exit_code}. "
                    f"stderr tail: {stderr[-2000:]} "
                    "State unchanged — inspect logs, then 'resume'."
                )
            try:
                payload = parse_control_result(stdout, phase)
            except (ControlResultError, ControlResultValidationError) as exc:
                record.error = f"{type(exc).__name__}: {exc}"
                self._logger().log_execution(record, prompt, stdout, stderr)
                self._save()
                if attempt <= max_corrections:
                    correction_error = f"{type(exc).__name__}: {exc}"
                    continue
                raise ControlResultValidationError(
                    f"agent '{profile.name}' did not return a valid CONTROL_RESULT after "
                    f"{attempt} attempt(s): {exc}"
                ) from exc
            record.parsed_result = payload
            self._logger().log_execution(record, prompt, stdout, stderr)
            return payload

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
        if issue.canonical != state.current_issue_url:
            raise VerificationError(
                f"agent reported issue {issue.canonical} but the run is for "
                f"{state.current_issue_url}"
            )
        pr_ref = parse_pr_url(res.pr_url)
        if pr_ref.repository.lower() != state.repository.lower():
            raise VerificationError(
                f"agent reported PR {pr_ref.canonical} outside repository {state.repository}"
            )
        try:
            pr = self.github.get_pr(pr_ref.canonical)
        except GitHubError as exc:
            raise VerificationError(
                f"agent reported PR {pr_ref.canonical} but GitHub cannot resolve it: {exc}"
            ) from exc
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
        state.current_branch = pr.head_ref or res.branch
        state.review_round = 0
        state.open_findings = []
        state.review_history = []
        state.last_review_result = ""
        state.reviewed_head_sha = ""
        return Phase.REVIEW, (
            f"PR {pr_ref.canonical} verified (HEAD {pr.head_sha[:12]}, branch "
            f"{state.current_branch}); ANALYZE_EXECUTE -> REVIEW"
        )

    def _verify_review_comment(self, res: ReviewResult, expected_head: str) -> None:
        state = self._require_state()
        try:
            cref = parse_comment_url(res.review_comment_url)
        except Exception as exc:
            raise VerificationError(
                f"review_comment_url is not a GitHub PR comment URL: {exc}"
            ) from exc
        pr_ref = parse_pr_url(state.current_pr_url)
        if cref.parent.canonical != pr_ref.canonical:
            raise VerificationError(
                f"review comment {res.review_comment_url} does not belong to PR {pr_ref.canonical}"
            )
        comments = self.github.get_pr_comments(pr_ref.canonical)
        match = None
        for c in comments:
            if c.id == cref.comment_id or (c.url and c.url == res.review_comment_url):
                match = c
                break
        if match is None:
            raise VerificationError(
                f"review comment {res.review_comment_url} was not found on PR {pr_ref.canonical}"
            )
        body = match.body or ""
        heading = _REVIEW_HEADING_RE.search(body)
        if heading is None or int(heading.group(1)) != res.round:
            raise VerificationError(
                f"review comment lacks the '# AI Code Review — Round {res.round}' heading"
            )
        marker = _REVIEW_MARKER_RE.search(body)
        if marker is None:
            raise VerificationError("review comment lacks the '<!-- ai-review-result -->' marker")
        try:
            data = json.loads(marker.group(1))
        except json.JSONDecodeError as exc:
            raise VerificationError(f"review comment marker is not valid JSON: {exc}") from exc
        if not isinstance(data, dict) or data.get("round") != res.round:
            got = data.get("round") if isinstance(data, dict) else data
            raise VerificationError(
                f"review comment marker round {got!r} != expected round {res.round}"
            )
        marker_sha = str(data.get("reviewed_head_sha", "")).lower()
        if marker_sha != expected_head:
            raise VerificationError(
                f"review comment marker reviewed_head_sha {marker_sha!r} "
                f"!= bound HEAD {expected_head}"
            )
        if "needs_fix_round" in data and data["needs_fix_round"] != res.needs_fix_round:
            raise VerificationError(
                "review comment marker needs_fix_round disagrees with CONTROL_RESULT"
            )

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
        self._verify_review_comment(res, expected_head)

        # Valid review lifecycle: round is consumed regardless of the outcome.
        state.review_round = res.round
        state.reviewed_head_sha = expected_head
        state.last_review_comment_url = res.review_comment_url
        state.last_review_needs_fix = res.needs_fix_round
        findings = [f.to_dict() for f in res.findings]

        latest = self._require_open_pr()
        if latest.head_sha != expected_head:
            state.current_head_sha = latest.head_sha
            state.last_review_result = "stale"
            state.open_findings = []
            self._record_review(res.round, expected_head, RESULT_STALE, findings)
            return Phase.REVIEW, (
                f"review round {res.round} completed for {expected_head[:12]} but PR HEAD moved "
                f"to {latest.head_sha[:12]} during the review; re-reviewing the latest HEAD"
            )
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
                # Only the decision is recorded here, together with the
                # revision it was made on. The checkpoint and the transaction
                # id are created by `_prepare_replan`, inside the
                # REPLAN_REEXECUTE step that owns them.
                state.replan_transaction = ReplanTransaction(
                    stage=ReplanStage.PENDING,
                    decision_head_sha=expected_head,
                    decision_branch=state.current_branch,
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
        """Append the completed round to ``review_history`` (bounded per PR)."""
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
        for r in res.resolutions:
            if r.resolution == "follow_up_created":
                ref = parse_issue_url(r.follow_up_issue_url)
                if ref.repository.lower() != state.repository.lower():
                    raise VerificationError(
                        f"follow-up issue {ref.canonical} for {r.finding_id} is outside "
                        f"{state.repository}"
                    )
                if ref.canonical == state.current_issue_url:
                    raise VerificationError(
                        f"follow-up for {r.finding_id} points at the current issue itself"
                    )
                try:
                    issue = self.github.get_issue(ref.canonical)
                except GitHubError as exc:
                    raise VerificationError(
                        f"follow-up issue {ref.canonical} for {r.finding_id} does not exist: {exc}"
                    ) from exc
                if not issue.is_open:
                    raise VerificationError(
                        f"follow-up issue {ref.canonical} for {r.finding_id} is {issue.state}"
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
        state.last_fix_resolutions = [r.to_dict() for r in res.resolutions]
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
        if parse_issue_url(res.issue_url).canonical != txn.issue_url:
            mismatch = f"issue_url {res.issue_url!r} does not match the replan issue"
        elif parse_pr_url(res.previous_pr_url).canonical != txn.source_pr_url:
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
          is conclusive: asking the agent again would repeat UPDATE_EPIC's
          GitHub writes while the controller still could not verify anything,
          so the run is BLOCKED immediately without another invocation.
        """
        state = self._require_state()
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
