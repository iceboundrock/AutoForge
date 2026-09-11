"""AutoForge command-line interface.

Commands:
  doctor  read-only environment checks (git, gh, auth, claude, opencode, repo, config)
  run     create a run (Issue -> ...) and advance it until READY_FOR_MERGE/DONE/BLOCKED/FAILED
  step    execute exactly one phase step from persisted state
  resume  continue a persisted run until a stop phase / max-steps
  status  show the persisted run summary (--json for machine output)
  local   LOCAL mode: feature Markdown -> implement -> review -> fix -> DONE,
          with no GitHub involved at any point (init / run / doctor)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import AutoForgeConfig, load_config_file
from .doctor import CheckResult, run_doctor, run_local_doctor
from .engine import ControllerEngine, StepOutcome, StepPlan
from .errors import (
    AutoForgeError,
    ConfigurationError,
    LockError,
    StateError,
    StateTransitionError,
)
from .local_workspace import init_feature_file
from .redaction import redact, redact_argv
from .replan_txn import ReplanTransaction
from .state import (
    AutoForgeState,
    StatePaths,
    load_state,
    quarantine_state_file,
    save_state,
)
from .transitions import TERMINAL_PHASES, Phase, WorkflowMode


def _positive_int(text: str) -> int:
    """argparse type for ``--max-steps``: an integer >= 1.

    Rejecting the value at parse time keeps ``run --max-steps 0`` from
    writing a fresh state file and then crashing in ``engine.run()``.
    """
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be >= 1, got {value}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="autoforge",
        description=(
            "AutoForge — deterministic orchestration layer for AI-driven "
            "software development (Issue → Claude Code implementation → PR → "
            "OpenCode review → Claude Code fix → ... → READY_FOR_MERGE)."
        ),
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--config", default=None, help="config file (.yaml/.yml/.toml/.json)")
    p.add_argument(
        "--state-dir",
        default=None,
        help="state directory (default: from config, else .autoforge)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="check git/gh/claude/opencode/repo/config (read-only)")
    d.add_argument("--json", action="store_true", dest="as_json")

    r = sub.add_parser("run", help="create a run and advance it")
    r.add_argument("--epic", required=True, help="EPIC issue URL")
    r.add_argument("--issue", required=True, help="first issue URL")
    r.add_argument("--dry-run", action="store_true", help="plan only; no side effects")
    r.add_argument(
        "--force",
        action="store_true",
        help=(
            "discard an existing non-terminal run; an unreadable state entry (bad JSON, "
            "foreign protocol, invalid UTF-8, dangling symlink, FIFO/socket/device) is "
            "moved aside as state.json.corrupt-<timestamp> instead of being deleted "
            "(a directory is refused and must be moved by hand)"
        ),
    )
    r.add_argument(
        "--max-steps", type=_positive_int, default=50, help="steps for this invocation (>= 1)"
    )
    r.add_argument(
        "--allow-merge",
        action="store_true",
        help="unlock MERGE execution (also needs config safety.allow_merge=true)",
    )
    r.add_argument(
        "--full-prompt", action="store_true", help="print full rendered prompt in dry-run"
    )

    s = sub.add_parser("step", help="execute exactly one step from persisted state")
    s.add_argument("--dry-run", action="store_true")
    s.add_argument("--allow-merge", action="store_true")
    s.add_argument("--full-prompt", action="store_true")

    re_ = sub.add_parser("resume", help="continue a persisted run")
    re_.add_argument("--dry-run", action="store_true")
    re_.add_argument(
        "--max-steps", type=_positive_int, default=50, help="steps for this invocation (>= 1)"
    )
    re_.add_argument("--allow-merge", action="store_true")
    re_.add_argument("--full-prompt", action="store_true")

    st = sub.add_parser("status", help="show persisted run status")
    st.add_argument("--json", action="store_true", dest="as_json")

    _add_local_parser(sub)
    return p


def _add_local_parser(sub) -> None:
    """`autoforge local ...`: the GitHub-free workflow.

    ``status``/``step``/``resume`` stay mode-agnostic (they read the mode from
    persisted state), so only the commands that *differ* live here.
    """
    loc = sub.add_parser(
        "local",
        help="local mode: drive a feature Markdown file with no GitHub involved",
        description=(
            "LOCAL mode: features/<slug>.md -> ANALYZE_EXECUTE -> REVIEW -> FIX -> "
            "REVIEW -> DONE, entirely in the working tree. No gh, no PR, no push, "
            "no merge, no commits. Use 'autoforge status/step/resume' as usual."
        ),
    )
    lsub = loc.add_subparsers(dest="local_command", required=True)

    li = lsub.add_parser("init", help="create features/<slug>.md from the template")
    li.add_argument("slug", help="feature slug, e.g. add-transaction-filter")
    li.add_argument(
        "--feature-dir",
        default=None,
        help="directory for the file (default: config local.feature_dir)",
    )
    li.add_argument(
        "--force",
        action="store_true",
        help="overwrite an existing feature file (refused by default)",
    )

    lr = lsub.add_parser(
        "run", help="create a local run from a feature Markdown file and advance it"
    )
    lr.add_argument("feature", help="path to the feature Markdown file (inside this repository)")
    lr.add_argument("--dry-run", action="store_true", help="plan only; no side effects")
    lr.add_argument(
        "--force",
        action="store_true",
        help="discard an existing non-terminal run (see 'run --force')",
    )
    lr.add_argument(
        "--max-steps", type=_positive_int, default=50, help="steps for this invocation (>= 1)"
    )
    lr.add_argument(
        "--allow-dirty",
        action="store_true",
        help=(
            "start even though the working tree has changes other than the feature file; "
            "they are recorded in the run and reported to the reviewer, never treated as "
            "part of the implementation"
        ),
    )
    lr.add_argument(
        "--full-prompt", action="store_true", help="print full rendered prompt in dry-run"
    )

    ld = lsub.add_parser(
        "doctor", help="check config/git/agent CLIs for local mode (never touches GitHub)"
    )
    ld.add_argument("--json", action="store_true", dest="as_json")
    ld.add_argument("--feature", default=None, help="also validate this feature Markdown path")


def _load_cfg(args) -> AutoForgeConfig:
    return load_config_file(args.config)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "step":
            return cmd_step(args)
        if args.command == "resume":
            return cmd_resume(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "local":
            if args.local_command == "init":
                return cmd_local_init(args)
            if args.local_command == "run":
                return cmd_local_run(args)
            if args.local_command == "doctor":
                return cmd_local_doctor(args)
            parser.error(f"unknown local command {args.local_command}")
            return 2
        parser.error(f"unknown command {args.command}")
        return 2
    except (ConfigurationError, StateError, StateTransitionError, LockError) as exc:
        print(f"autoforge: error: {redact(str(exc))}", file=sys.stderr)
        return 2
    except AutoForgeError as exc:
        print(f"autoforge: error: {redact(str(exc))}", file=sys.stderr)
        return 1


# -- commands ---------------------------------------------------------------
def _engine_for(args, cfg: AutoForgeConfig | None = None) -> ControllerEngine:
    cfg = cfg or _load_cfg(args)
    state_dir = args.state_dir or cfg.state_dir
    return ControllerEngine(config=cfg, state_dir=state_dir)


def _local_engine_for(args, cfg: AutoForgeConfig | None = None) -> ControllerEngine:
    """An engine for a LOCAL command, with its state directory resolved.

    LOCAL state never lives in the reviewed working tree; see
    :meth:`ControllerEngine.bind_local_state_dir`.
    """
    engine = _engine_for(args, cfg)
    engine.bind_local_state_dir(args.state_dir)
    return engine


def _bind_existing_run(engine: ControllerEngine) -> None:
    """Resolve which run a mode-agnostic command (`resume`, `step`, `status`) means.

    A REMOTE run keeps its state under `config.state_dir` (`.autoforge` by
    default); a LOCAL run keeps it in the git directory, because the LOCAL
    fingerprint covers the working tree. The two locations are disjoint and
    the mode is not known until state is read, so the command looks for a
    `state.json` in both. Exactly one match is used. Two matches is a real
    ambiguity and is refused rather than resolved by precedence: picking one
    would silently `resume` the wrong workflow. Zero matches leaves the
    configured location so the error names it.

    `--state-dir` is an answer to this question, so it is never second-guessed
    (the caller does not reach here when it was given).
    """
    configured = engine.paths
    try:
        local = engine.local_state_paths()
    except (AutoForgeError, OSError):
        # Not a git repository (or git is unusable): only the configured
        # location can hold a run, and any real problem surfaces there.
        return
    if os.path.abspath(local.state_dir) == os.path.abspath(configured.state_dir):
        return
    here = os.path.lexists(configured.state_file)
    there = os.path.lexists(local.state_file)
    if here and there:
        raise StateError(
            f"two runs exist: a remote-style one at {configured.state_file} and a local "
            f"one at {local.state_file}. Pass --state-dir to say which one this command "
            "means; the controller will not pick for you."
        )
    if there:
        engine.paths = local


def _doctor_runner():
    """Command runner used by `doctor` (indirection so tests can inject a fake)."""
    return None


def cmd_doctor(args) -> int:
    results = run_doctor(config_path=args.config, state_dir=args.state_dir, runner=_doctor_runner())
    return _report_checks(results, args.as_json, "AutoForge doctor (read-only checks)")


def cmd_local_doctor(args) -> int:
    """`autoforge local doctor`: no gh, no gh auth, no 'origin' remote."""
    results = run_local_doctor(
        config_path=args.config,
        state_dir=args.state_dir,
        runner=_doctor_runner(),
        feature_spec_path=args.feature,
    )
    return _report_checks(results, args.as_json, "AutoForge doctor — local mode (read-only checks)")


def _report_checks(results: list[CheckResult], as_json: bool, title: str) -> int:
    if as_json:
        checks = [
            {"name": r.name, "ok": r.ok, "required": r.required, "detail": redact(r.detail)}
            for r in results
        ]
        print(
            json.dumps(
                {"ok": all(c["ok"] or not c["required"] for c in checks), "checks": checks},
                indent=2,
            )
        )
    else:
        print(title)
        print()
        for r in results:
            print(f"  [{r.label}] {r.name}: {redact(r.detail)}")
        print()
    failed = [r for r in results if not r.ok and r.required]
    if failed:
        print(f"{len(failed)} required check(s) failed.", file=sys.stderr)
        return 1
    print("All checks passed.")
    return 0


def _finish(engine: ControllerEngine, outcomes: list[StepOutcome], allow_merge: bool) -> int:
    for o in outcomes:
        print_step_outcome(o)
    state = engine.state
    assert state is not None
    if state.phase == Phase.READY_FOR_MERGE:
        print_ready_banner(state, gate_open=engine.merge_gate_open(allow_merge))
        return 0
    if state.phase in (Phase.BLOCKED, Phase.FAILED):
        print(f"\nrun {state.run_id} is {state.phase.value}: {state.block_reason or '-'}")
        return 1
    return 0


def _existing_run_guard(
    paths: StatePaths, force: bool, command: str = "run"
) -> tuple[int | None, bool]:
    """Decide whether a fresh run may overwrite ``paths.state_file``.

    Returns ``(exit_code_or_None, corrupt)``. Must be called with the
    controller lock held: a verdict taken before the lock could go stale.

    ``command`` is the subcommand the operator actually typed, so the advice
    names a command that can be copied (`run --force`, `local run --force`)
    rather than a bare flag.
    """
    # lexists, not exists: a dangling state.json symlink is still an entry
    # that a fresh save would silently replace.
    if not os.path.lexists(paths.state_file):
        return None, False
    try:
        existing = load_state(paths.state_file)
    except StateError as exc:
        # Unreadable / foreign-protocol state is fatal: a fresh run must
        # never silently replace it (merge counters etc. would be lost).
        if not force:
            print(
                f"autoforge: error: {exc}\n"
                "autoforge: error: refusing to start a new run over an unreadable "
                f"state file — repair it, or use '{command} --force' to move it aside as "
                f"{paths.state_file.name}.corrupt-<timestamp> and start over",
                file=sys.stderr,
            )
            return 2, False
        return None, True
    if existing.phase not in TERMINAL_PHASES and not force:
        print(
            f"autoforge: error: existing run {existing.run_id} "
            f"in phase {existing.phase.value} — use 'resume' to continue "
            f"or '{command} --force' to discard it",
            file=sys.stderr,
        )
        return 2, False
    return None, False


def cmd_run(args) -> int:
    cfg = _load_cfg(args)
    engine = _engine_for(args, cfg)
    engine.validate_config()
    paths = engine.paths

    if args.dry_run:
        # Fully side-effect-free: in-memory state, plan printed, nothing written.
        engine.new_run(args.epic, args.issue)
        outcomes = engine.run(max_steps=args.max_steps, dry_run=True, allow_merge=args.allow_merge)
        for o in outcomes:
            assert o.plan is not None
            print_plan(o.plan, full_prompt=args.full_prompt)
        return 0

    # Inspect, decide, quarantine, write the first state AND execute under
    # one continuous lock: a verdict taken before the lock could go stale
    # (another controller may have repaired, replaced or created state.json
    # in the meantime) and would then quarantine or overwrite a perfectly
    # valid run; releasing the lock between the first save and the execution
    # would let a second controller take over the repository and both would
    # then run agents and persist state over each other.
    with engine.locked():
        rc, corrupt = _existing_run_guard(paths, args.force)
        if rc is not None:
            return rc
        engine.new_run(args.epic, args.issue)
        assert engine.state is not None
        if corrupt:
            moved = quarantine_state_file(paths.state_file)
            print(f"autoforge: moved unreadable state file aside: {moved}", file=sys.stderr)
        save_state(engine.state, paths.state_file)
        outcomes = engine.run(max_steps=args.max_steps, dry_run=False, allow_merge=args.allow_merge)
    return _finish(engine, outcomes, args.allow_merge)


def cmd_local_init(args) -> int:
    """Create features/<slug>.md under the repository lock. No --force, no overwrite.

    The controller lock covers this the same way it covers `local run`. A
    feature specification is not a scratch file: a run freezes its SHA-256 and
    re-checks it around every phase, so `local init --force` racing an active
    run could rewrite the specification while the controller was hashing it,
    rendering it into a prompt or verifying it — which is precisely the
    "an agent cannot rewrite its own acceptance criteria" guarantee, undone
    from the operator's side. One controller at a time owns the repository,
    and creating this file is a controller write like any other.
    """
    cfg = _load_cfg(args)
    engine = _local_engine_for(args, cfg)
    with engine.locked():
        path = init_feature_file(
            engine.workspace(),
            args.slug,
            feature_dir=args.feature_dir or cfg.local.feature_dir,
            overwrite=args.force,
        )
    rel = os.path.relpath(path, os.getcwd())
    print(f"Created {rel}")
    print()
    print("Next:")
    print(f"  1. Describe the feature in {rel} (problem, requirements, acceptance criteria).")
    print(f"  2. autoforge local run {rel}")
    print()
    print("The specification is frozen (SHA-256) when the run starts: edit it before, not during.")
    return 0


def cmd_local_run(args) -> int:
    """Create a LOCAL run from a feature Markdown file and advance it.

    Mirrors ``cmd_run`` minus everything GitHub: no EPIC, no issue, no merge
    gate. The engine never constructs a GitHubClient for a LOCAL run.
    """
    cfg = _load_cfg(args)
    engine = _local_engine_for(args, cfg)
    paths = engine.paths

    if args.dry_run:
        # Fully side-effect-free: in-memory state, plan printed, nothing
        # written, no agent invoked and no validation command executed.
        engine.new_local_run(args.feature, allow_dirty=args.allow_dirty)
        engine.validate_config()
        outcomes = engine.run(max_steps=args.max_steps, dry_run=True)
        for o in outcomes:
            assert o.plan is not None
            print_plan(o.plan, full_prompt=args.full_prompt)
        return 0

    # One continuous lock over inspect -> decide -> first save -> execute,
    # for the same reasons as `cmd_run`.
    with engine.locked():
        rc, corrupt = _existing_run_guard(paths, args.force, "local run")
        if rc is not None:
            return rc
        engine.new_local_run(args.feature, allow_dirty=args.allow_dirty)
        engine.validate_config()
        assert engine.state is not None
        if corrupt:
            moved = quarantine_state_file(paths.state_file)
            print(f"autoforge: moved unreadable state file aside: {moved}", file=sys.stderr)
        save_state(engine.state, paths.state_file)
        outcomes = engine.run(max_steps=args.max_steps, dry_run=False)
    return _finish(engine, outcomes, allow_merge=False)


def cmd_step(args) -> int:
    engine = _engine_for(args)
    if not args.state_dir:
        _bind_existing_run(engine)
    if args.dry_run:
        # Read-only: no lock, nothing written.
        engine.load()
        outcome = engine.step(dry_run=True, allow_merge=args.allow_merge)
        if outcome.plan is not None:
            print_plan(outcome.plan, full_prompt=args.full_prompt)
        return 0
    # Load and execute under one lock: a snapshot loaded before the lock
    # could already have been replaced by another controller.
    with engine.locked():
        engine.load()
        outcome = engine.step(dry_run=False, allow_merge=args.allow_merge)
    return _finish(engine, [outcome], args.allow_merge)


def cmd_resume(args) -> int:
    engine = _engine_for(args)
    if not args.state_dir:
        _bind_existing_run(engine)
    if args.dry_run:
        # Read-only: no lock, nothing written.
        state = engine.load()
        rc = _resume_holding_state(engine, state, args.allow_merge)
        if rc is not None:
            return rc
        outcomes = engine.run(max_steps=args.max_steps, dry_run=True, allow_merge=args.allow_merge)
        for o in outcomes:
            assert o.plan is not None
            print_plan(o.plan, full_prompt=args.full_prompt)
        return 0
    # Load, decide and execute under one lock: a snapshot loaded before the
    # lock could already have been replaced by another controller.
    with engine.locked():
        state = engine.load()
        rc = _resume_holding_state(engine, state, args.allow_merge)
        if rc is not None:
            return rc
        outcomes = engine.run(max_steps=args.max_steps, dry_run=False, allow_merge=args.allow_merge)
    return _finish(engine, outcomes, args.allow_merge)


def _resume_holding_state(
    engine: ControllerEngine, state: AutoForgeState, allow_merge: bool
) -> int | None:
    """Exit code when ``resume`` has nothing to execute; None when it does."""
    if state.phase == Phase.READY_FOR_MERGE and not engine.merge_gate_open(allow_merge):
        # Holding state: nothing runs unless the merge gate is open. With the
        # gate open, engine.run() performs the controller-side pre-merge
        # verification instead (bounded re-checks of inconclusive GitHub data
        # happen there, one attempt per resume).
        print_ready_banner(state)
        return 0
    if state.phase in TERMINAL_PHASES:
        if state.phase == Phase.DONE:
            print(f"workflow already DONE (run {state.run_id}) — nothing to do")
            return 0
        print(
            f"run {state.run_id} is in terminal phase {state.phase.value}: "
            f"{state.block_reason or '-'} — inspect {engine.paths.logs_dir}/ "
            "and start a new run"
        )
        return 1
    return None


def cmd_status(args) -> int:
    engine = _engine_for(args)
    if not args.state_dir:
        _bind_existing_run(engine)
    state = engine.load()  # StateError when missing/corrupt
    if args.as_json:
        # to_dict() already carries "mode" plus the local fields, so the JSON
        # shape stays additive for remote consumers.
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0
    if state.mode == WorkflowMode.LOCAL:
        return _print_local_status(state)

    def _num(url: str) -> str:
        return url.rstrip("/").rsplit("/", 1)[-1] if url else "-"

    print("AutoForge")
    print()
    print(f"Run:        {state.run_id}")
    print(f"Repository: {state.repository}")
    print(f"EPIC:       #{_num(state.epic_url)} {state.epic_url}")
    print(f"Issue:      #{_num(state.current_issue_url)} {state.current_issue_url or '-'}")
    print(f"PR:         #{_num(state.current_pr_url)} {state.current_pr_url or '-'}")
    print(f"Branch:     {state.current_branch or '-'}")
    print()
    print(f"Phase:      {state.phase.value}")
    print(f"Review rounds completed: {state.review_round}")
    print(f"Execution attempt: {state.execution_attempt}")
    print(f"Replan count: {state.escalation_count}")
    print(f"Current HEAD:  {state.current_head_sha or '-'}")
    print(f"Reviewed HEAD: {state.reviewed_head_sha or '-'}")
    print(f"Last review:   {state.last_review_result or '-'}")
    print(f"Review comment: {state.last_review_comment_url or '-'}")
    print(f"Open findings: {len(state.open_findings)}")
    if state.superseded_prs:
        print("Superseded PRs:")
        for item in state.superseded_prs:
            print(f"  {item.get('pr_url', '-')} -> {item.get('replacement_pr_url', '-')}")
    if state.replan_transaction:
        txn = ReplanTransaction.from_dict(state.replan_transaction)
        print(f"Replan transaction: {txn.transaction_id or '(not yet created)'}")
        print(f"  stage:       {txn.stage.value}")
        print(f"  source PR:   {txn.source_pr_url or '-'}")
        print(f"  replacement: {txn.replacement_pr_url or '-'}")
        print(f"  escalation:  {json.dumps(txn.escalation, sort_keys=True)}")
        if txn.rejection_reason:
            print(f"  rejected:    {txn.rejection_reason}")
    if state.block_reason:
        print(f"Reason:     {state.block_reason}")
    print()
    print(f"Steps executed: {state.step_count}")
    print(f"Merged since EPIC update: {state.merged_since_epic_update}")
    print()
    print(f"Created:    {state.created_at}")
    print(f"Updated:    {state.updated_at}")
    return 0


def _print_local_status(state: AutoForgeState) -> int:
    """Human status for a LOCAL run.

    Deliberately omits Issue/PR/branch/merge fields: a local run has none, and
    printing empty ones would suggest the GitHub lifecycle is merely stalled.
    """
    fix_budget = state.local_fix_rounds

    print("AutoForge (local mode)")
    print()
    print(f"Run:        {state.run_id}")
    print(f"Mode:       {state.mode.value} (no GitHub: no issue, no PR, no push, no merge)")
    print(f"Feature:    {state.feature_spec_path or '-'}")
    print(f"Frozen SHA: {state.feature_spec_sha256 or '-'}")
    print()
    print(f"Phase:      {state.phase.value}")
    print(f"Review rounds completed: {state.review_round}")
    print(f"Fix rounds completed:    {fix_budget}")
    print(f"Last review:   {state.last_review_result or '-'}")
    print(f"Open findings: {len(state.open_findings)}")
    for finding in state.open_findings:
        print(
            f"  {finding.get('id', '?')} [{finding.get('severity', '?')}] "
            f"{str(finding.get('title', '')).strip()[:80]}"
        )
    print()
    print(f"Base HEAD:   {state.base_head_sha or '(unborn)'}")
    print(f"Workspace fingerprint: {state.workspace_fingerprint or '-'}")
    print(f"Reviewed fingerprint:  {state.reviewed_workspace_fingerprint or '-'}")
    if state.baseline_dirty_paths:
        print("Pre-existing working-tree changes at run creation (--allow-dirty):")
        for path in state.baseline_dirty_paths[:20]:
            print(f"  {path}")
        if len(state.baseline_dirty_paths) > 20:
            print(f"  ... and {len(state.baseline_dirty_paths) - 20} more")
    if state.block_reason:
        print(f"Reason:     {state.block_reason}")
    print()
    print(f"Steps executed: {state.step_count}")
    print()
    print(f"Created:    {state.created_at}")
    print(f"Updated:    {state.updated_at}")
    return 0


# -- pretty printing ----------------------------------------------------------
def print_ready_banner(state: AutoForgeState, gate_open: bool = False) -> None:
    print()
    print("=" * 72)
    print("AutoForge workflow reached READY_FOR_MERGE.")
    print(f"  Issue:         {state.current_issue_url}")
    print(f"  PR:            {state.current_pr_url}")
    print(f"  Review round:  {state.review_round}")
    print(f"  Reviewed HEAD: {state.reviewed_head_sha}")
    print(f"  Review:        {state.last_review_comment_url or '-'}")
    if gate_open:
        print("Merge gate is open but the step budget (--max-steps) ran out before MERGE.")
        print("'resume --allow-merge' continues with the controller-side pre-merge verification.")
    else:
        print("Automatic merge is disabled in this milestone.")
        print("A human must review and merge the PR.")
    print("=" * 72)


def print_plan(plan: StepPlan, full_prompt: bool = False) -> None:
    print(f"Phase:    {plan.phase}")
    print(
        f"Profile:  {plan.profile_name} "
        f"(provider={plan.provider}, model={plan.model}, effort={plan.effort})"
    )
    if plan.review_round and plan.phase in ("REVIEW", "FIX"):
        print(f"Review round: {plan.review_round}")
    if plan.template:
        print(f"Template: {plan.template}")
    if plan.command:
        print(f"Command:  {' '.join(redact_argv(plan.command)[:-1])} <prompt>")
        print(f"Timeout:  {plan.timeout_seconds}s")
    print(f"Routing:  {plan.routing}")
    print(f"Expected next: {plan.expected_next or '-'}")
    if plan.legal_next:
        print(f"Legal next: {', '.join(plan.legal_next)}")
    for note in plan.notes:
        print(f"Note:     {note}")
    if plan.variables:
        print("Variables:")
        for k, v in sorted(plan.variables.items()):
            text = redact(v).replace("\n", " ")
            print(f"  {k} = {text[:120]}")
    if plan.prompt_length:
        print(f"Prompt:   {plan.prompt_length} chars")
        print("--- prompt preview ---")
        print(redact(plan.prompt_preview if not full_prompt else plan.prompt_full))
        print("--- end preview ---")
    print()


def print_step_outcome(o: StepOutcome) -> None:
    print(f"[{o.run_id}] {o.previous_phase} -> {o.next_phase}: {redact(o.message)}")
    if o.plan is not None and o.plan.command:
        print(f"  profile={o.plan.profile_name} model={o.plan.model}")
    if o.result is not None:
        print(f"  result={redact(json.dumps(o.result, sort_keys=True))}")


if __name__ == "__main__":
    raise SystemExit(main())
