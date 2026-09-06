"""AutoForge command-line interface.

Commands:
  doctor  read-only environment checks (git, gh, auth, claude, opencode, repo, config)
  run     create a run (Issue -> ...) and advance it until READY_FOR_MERGE/DONE/BLOCKED/FAILED
  step    execute exactly one phase step from persisted state
  resume  continue a persisted run until a stop phase / max-steps
  status  show the persisted run summary (--json for machine output)
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .config import AutoForgeConfig, load_config_file
from .doctor import run_doctor
from .engine import ControllerEngine, StepOutcome, StepPlan
from .errors import (
    AutoForgeError,
    ConfigurationError,
    LockError,
    StateError,
    StateTransitionError,
)
from .locking import ControllerLock
from .redaction import redact, redact_argv
from .state import (
    AutoForgeState,
    StatePaths,
    load_state,
    quarantine_state_file,
    save_state,
)
from .transitions import TERMINAL_PHASES, Phase


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
            "foreign protocol, invalid UTF-8, dangling symlink) is moved aside as "
            "state.json.corrupt-<timestamp> instead of being deleted"
        ),
    )
    r.add_argument("--max-steps", type=int, default=50)
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
    re_.add_argument("--max-steps", type=int, default=50)
    re_.add_argument("--allow-merge", action="store_true")
    re_.add_argument("--full-prompt", action="store_true")

    st = sub.add_parser("status", help="show persisted run status")
    st.add_argument("--json", action="store_true", dest="as_json")
    return p


def _resolve_state_dir(args) -> str:
    if args.state_dir:
        return args.state_dir
    if args.config:
        cfg = load_config_file(args.config)
        return cfg.state_dir
    return ".autoforge"


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


def _doctor_runner():
    """Command runner used by `doctor` (indirection so tests can inject a fake)."""
    return None


def cmd_doctor(args) -> int:
    results = run_doctor(config_path=args.config, state_dir=args.state_dir, runner=_doctor_runner())
    if args.as_json:
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
        print("AutoForge doctor (read-only checks)")
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

    # Inspect, decide, quarantine and write the first state under one lock:
    # a verdict taken before the lock could go stale (another controller may
    # have repaired, replaced or created state.json in the meantime) and
    # would then quarantine or overwrite a perfectly valid run.
    with ControllerLock(paths.lock_file):
        corrupt = False
        # lexists, not exists: a dangling state.json symlink is still an
        # entry that a fresh save would silently replace.
        if os.path.lexists(paths.state_file):
            try:
                existing = load_state(paths.state_file)
            except StateError as exc:
                # Unreadable / foreign-protocol state is fatal: a fresh run
                # must never silently replace it (merge counters etc. would
                # be lost).
                if not args.force:
                    print(
                        f"autoforge: error: {exc}\n"
                        "autoforge: error: refusing to start a new run over an unreadable "
                        "state file — repair it, or use 'run --force' to move it aside as "
                        f"{paths.state_file.name}.corrupt-<timestamp> and start over",
                        file=sys.stderr,
                    )
                    return 2
                corrupt = True
            else:
                if existing.phase not in TERMINAL_PHASES and not args.force:
                    print(
                        f"autoforge: error: existing run {existing.run_id} "
                        f"in phase {existing.phase.value} — use 'resume' to continue "
                        "or 'run --force' to discard it",
                        file=sys.stderr,
                    )
                    return 2
        engine.new_run(args.epic, args.issue)
        assert engine.state is not None
        if corrupt:
            moved = quarantine_state_file(paths.state_file)
            print(f"autoforge: moved unreadable state file aside: {moved}", file=sys.stderr)
        save_state(engine.state, paths.state_file)
    outcomes = engine.run(max_steps=args.max_steps, dry_run=False, allow_merge=args.allow_merge)
    return _finish(engine, outcomes, args.allow_merge)


def cmd_step(args) -> int:
    engine = _engine_for(args)
    engine.load()
    outcome = engine.step(dry_run=args.dry_run, allow_merge=args.allow_merge)
    if args.dry_run and outcome.plan is not None:
        print_plan(outcome.plan, full_prompt=args.full_prompt)
        return 0
    return _finish(engine, [outcome], args.allow_merge)


def cmd_resume(args) -> int:
    engine = _engine_for(args)
    state = engine.load()
    if state.phase == Phase.READY_FOR_MERGE and not engine.merge_gate_open(args.allow_merge):
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
            f"{state.block_reason or '-'} — inspect .autoforge/logs/ and start a new run"
        )
        return 1
    if args.dry_run:
        outcomes = engine.run(max_steps=args.max_steps, dry_run=True, allow_merge=args.allow_merge)
        for o in outcomes:
            assert o.plan is not None
            print_plan(o.plan, full_prompt=args.full_prompt)
        return 0
    outcomes = engine.run(max_steps=args.max_steps, dry_run=False, allow_merge=args.allow_merge)
    return _finish(engine, outcomes, args.allow_merge)


def cmd_status(args) -> int:
    state_dir = _resolve_state_dir(args)
    paths = StatePaths.from_state_dir(state_dir)
    state = load_state(paths.state_file)  # StateError when missing/corrupt
    if args.as_json:
        print(json.dumps(state.to_dict(), indent=2, sort_keys=True))
        return 0

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
    print(f"Current HEAD:  {state.current_head_sha or '-'}")
    print(f"Reviewed HEAD: {state.reviewed_head_sha or '-'}")
    print(f"Last review:   {state.last_review_result or '-'}")
    print(f"Review comment: {state.last_review_comment_url or '-'}")
    print(f"Open findings: {len(state.open_findings)}")
    if state.block_reason:
        print(f"Reason:     {state.block_reason}")
    print()
    print(f"Steps executed: {state.step_count}")
    print(f"Merged since EPIC update: {state.merged_since_epic_update}")
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
