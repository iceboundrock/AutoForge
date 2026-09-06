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
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from . import __prompt_version__
from .config import AutoForgeConfig, validate_required_profiles
from .errors import (
    ControlResultError,
    ControlResultValidationError,
    ExecutionError,
    ExecutionTimeoutError,
    GitHubError,
    StateError,
    StateTransitionError,
    VerificationError,
)
from .github import GitHubClient, PRInfo
from .locking import ControllerLock
from .profiles import profile_for_phase
from .prompts import TEMPLATE_FILES, load_template, render, render_phase
from .providers import AgentExecutionResult, AgentRequest, ProviderRegistry
from .result_parser import (
    AnalyzeExecuteResult,
    FixResult,
    MergeResult,
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
]

MERGE_GATE_MESSAGE = (
    "Automatic merge is disabled in this milestone: MERGE requires BOTH config "
    "'safety.allow_merge: true' AND the CLI flag '--allow-merge'."
)

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
    Phase.READY_FOR_MERGE: None,  # holding state, no agent call
    Phase.MERGE: "merge.md",
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
        return self.state

    def load(self) -> AutoForgeState:
        self.state = load_state(self.paths.state_file)
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
        return {
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
        }

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
                "(holds unless merge gate is opened)",
                notes=[MERGE_GATE_MESSAGE],
            )
        profile = profile_for_phase(self.config, s.phase, s.review_round)
        variables = self._prompt_variables()
        prompt = self.render_prompt_for(s.phase)
        command = self.providers.get(profile).build_command_for(profile, prompt)
        notes: list[str] = []
        if s.phase == Phase.ANALYZE_EXECUTE:
            notes.append("would first check for an existing open PR (recovery -> REVIEW)")
        if s.phase == Phase.REVIEW:
            notes.append("REVIEWED_HEAD_SHA is fetched from gh immediately before the review")
        if s.phase == Phase.MERGE:
            notes.append(MERGE_GATE_MESSAGE)
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

    @staticmethod
    def _deterministic_plan(
        state: AutoForgeState, routing: str, expected: str, notes: list[str]
    ) -> StepPlan:
        return StepPlan(
            phase=state.phase.value,
            profile_name="(none — deterministic transition)",
            provider="(none)",
            model="(none)",
            effort="(none)",
            command=[],
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
            Phase.MERGE: "ANALYZE_EXECUTE | UPDATE_EPIC | DONE",
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
    def step(self, dry_run: bool = False, allow_merge: bool = False) -> StepOutcome:
        if dry_run:
            return self._step_once(dry_run=True, allow_merge=allow_merge)
        with ControllerLock(self.paths.lock_file):
            return self._step_once(dry_run=False, allow_merge=allow_merge)

    def run(
        self,
        max_steps: int = 50,
        dry_run: bool = False,
        allow_merge: bool = False,
    ) -> list[StepOutcome]:
        """Loop ``step()`` until a STOP phase or ``max_steps``."""
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
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
        with ControllerLock(self.paths.lock_file):
            for _ in range(max_steps):
                assert self.state is not None
                if self.state.phase in STOP_PHASES:
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

        state.step_count += 1
        if previous == Phase.INITIALIZING:
            return self._initialize(plan)
        if previous == Phase.READY_FOR_MERGE:
            return self._ready_for_merge_step(plan, allow_merge)
        if previous == Phase.MERGE:
            self._check_merge_gate(allow_merge)
        if previous == Phase.ANALYZE_EXECUTE:
            recovered = self._try_recover_pr()
            if recovered is not None:
                return recovered
        if previous == Phase.REVIEW:
            self._bind_review_head()
        if previous == Phase.FIX:
            self._prepare_fix()

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
        except (VerificationError, ControlResultValidationError):
            # The agent ran (and may have changed GitHub) but its claims did not
            # verify. Persist the attempt so `resume` re-enters this phase from
            # real state instead of pretending nothing happened.
            self._save()
            raise
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
        issue = self.github.get_issue(state.current_issue_url)
        if issue.repository and issue.repository.lower() != state.repository.lower():
            raise VerificationError(
                f"issue {state.current_issue_url} belongs to {issue.repository!r}, "
                f"not {state.repository!r}"
            )
        if not issue.is_open:
            raise VerificationError(
                f"issue {state.current_issue_url} is {issue.state}; only OPEN issues can be run"
            )
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
        state = self._require_state()
        self._check_merge_gate(allow_merge)
        pr = self._require_open_pr()
        if pr.head_sha != state.reviewed_head_sha:
            state.current_head_sha = pr.head_sha
            state.last_review_result = "stale"
            state.open_findings = []
            validate_transition(Phase.READY_FOR_MERGE, Phase.REVIEW)
            state.phase = Phase.REVIEW
            self._save()
            return self._outcome(
                Phase.READY_FOR_MERGE,
                plan=plan,
                message="PR HEAD moved after the clean review; READY_FOR_MERGE -> REVIEW",
            )
        validate_transition(Phase.READY_FOR_MERGE, Phase.MERGE)
        state.phase = Phase.MERGE
        self._save()
        return self._outcome(
            Phase.READY_FOR_MERGE, plan=plan, message="merge gate open; READY_FOR_MERGE -> MERGE"
        )

    def _check_merge_gate(self, allow_merge: bool) -> None:
        if not (self.config.merge_allowed_by_config and allow_merge):
            raise VerificationError(MERGE_GATE_MESSAGE)

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
        if phase == Phase.MERGE:
            return self._apply_merge(MergeResult.from_payload(payload))
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
            return Phase.REVIEW, (
                f"review round {res.round} completed for {expected_head[:12]} but PR HEAD moved "
                f"to {latest.head_sha[:12]} during the review; re-reviewing the latest HEAD"
            )
        if res.needs_fix_round:
            state.last_review_result = "needs_fix"
            state.open_findings = findings
            return Phase.FIX, (
                f"review round {res.round}: {len(findings)} finding(s); REVIEW -> FIX"
            )
        state.last_review_result = "clean"
        state.open_findings = []
        return Phase.READY_FOR_MERGE, (
            f"review round {res.round} clean for HEAD {expected_head[:12]}; "
            "REVIEW -> READY_FOR_MERGE"
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

    def _apply_merge(self, res: MergeResult) -> tuple[Phase, str]:
        state = self._require_state()
        if res.head_changed_after_review:
            return Phase.REVIEW, "HEAD changed after review; MERGE -> REVIEW"
        if res.merged and state.current_pr_url:
            state.record_merge(state.current_pr_url)
        if res.next_action == "NEXT_ISSUE" and res.next_issue_url:
            state.reset_for_new_issue(parse_issue_url(res.next_issue_url).canonical)
            return Phase.ANALYZE_EXECUTE, "MERGE -> ANALYZE_EXECUTE (next issue)"
        if res.next_action == "UPDATE_EPIC":
            return Phase.UPDATE_EPIC, "MERGE -> UPDATE_EPIC"
        return Phase.DONE, "MERGE -> DONE"

    def _apply_update_epic(self, res: UpdateEpicResult) -> tuple[Phase, str]:
        state = self._require_state()
        state.record_epic_update()
        if res.next_issue_url:
            state.reset_for_new_issue(parse_issue_url(res.next_issue_url).canonical)
            return Phase.ANALYZE_EXECUTE, "UPDATE_EPIC -> ANALYZE_EXECUTE"
        return Phase.DONE, "UPDATE_EPIC -> DONE"


def check_template_files() -> list[str]:
    """Return sorted names of missing prompt templates (empty == all good)."""
    missing = []
    for name in TEMPLATE_FILES:
        if not (Path(__file__).parent / "prompts" / name).exists():
            missing.append(name)
    return missing
