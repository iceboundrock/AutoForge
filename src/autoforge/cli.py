"""AutoForge command-line interface.

Commands:
  doctor  read-only environment checks (git, gh, auth, claude, opencode, repo, config,
          default-branch required checks)
  run     create a run (Issue -> ...) and advance it until READY_FOR_MERGE/DONE/BLOCKED/FAILED
  step    execute exactly one phase step from persisted state
  resume  continue a persisted run until a stop phase / max-steps; step, resume and
          unblock refuse a REMOTE run whose recorded configuration (profile
          routing, agent worktree root) this invocation's differs from, unless
          --accept-config-change (#219)
  unblock the operator's explicit exit from BLOCKED: the controller re-inspects
          live GitHub state, re-enters a phase through validate_transition or
          refuses and stays BLOCKED; the reason is recorded in state and the log.
          --approve-action / --decline-action decide on an acceptance action
          a FIX plan awaits (#211)
  status  show the persisted run summary (--json for machine output; --action
          prints an acceptance action's exact payload)
  local   LOCAL mode: feature Markdown -> implement -> review -> fix -> DONE,
          with no GitHub involved at any point (init / run / doctor)
  epic    the parallel EPIC scheduler (ADR 0008, #183), REMOTE only: run (with
          --dry-run and --force), resume, status (lock-free, --json), stop
          (lock-free, --now) and archive; every 'epic' command refuses
          --state-dir, since each issue's state lives in its scheduler namespace,
          and an EPIC of another repository than the checkout's 'origin'.
          'epic _worker' is the scheduler's hidden worker entry point
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Callable

from . import __version__
from .certificate import NOT_A_PERMISSION
from .certificate import describe as describe_certificate
from .config import MAX_PARALLEL_BOUNDS, AutoForgeConfig, load_config_file
from .config_fingerprint import RecordedConfiguration
from .doctor import CheckResult, run_doctor, run_local_doctor
from .effects import (
    AUTHORIZATION_POLICY,
    MAX_EFFECT_ATTEMPTS,
    EffectKind,
    EffectRecord,
    Stage,
    sha256_text,
)
from .engine import (
    ControllerEngine,
    StepOutcome,
    StepPlan,
    UnblockOutcome,
    sandbox_mode_summary,
)
from .epic_commands import (
    DRAIN_HELP,
    EXIT_OPEN_CLAIMS,
    archive_issue,
    checkout_epic,
    claims_left_open,
    collect_status,
    coordination_root_of,
    dry_run_plan,
    epic_run_guard,
    left_open_lines,
    stop_scheduler,
)
from .epic_graph import GraphReader
from .errors import (
    AutoForgeError,
    ConfigurationError,
    LockError,
    StateError,
    StateTransitionError,
)
from .evidence import (
    MAX_FAILURE_COUNT,
    LocalCandidate,
    Outcome,
    RemoteCandidate,
    parse_marker,
    record_summary,
)
from .github import GitHubClient
from .local_workspace import init_feature_file
from .locking import ControllerLock, repository_lock_path
from .ownership import refuse_scheduler_owned_issue, standalone_ownership_predicate
from .redaction import redact, redact_argv, redact_dict
from .replan_txn import ReplanTransaction
from .run_namespace import namespace_issue_of_state_dir
from .scheduler import EpicScheduler, SchedulerInputs
from .scheduler_stop import StopMode
from .state import AutoForgeState
from .transitions import TERMINAL_PHASES, Phase, WorkflowMode
from .validation import parse_issue_url


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


def _max_parallel(text: str) -> int:
    """argparse type for ``--max-parallel``: within ``scheduler.max_parallel``'s bounds."""
    low, high = MAX_PARALLEL_BOUNDS
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid int value: {text!r}") from None
    if not low <= value <= high:
        raise argparse.ArgumentTypeError(
            f"must be between {low} and {high}, got {value} (there is no unbounded mode)"
        )
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
    d.add_argument(
        "--epic",
        default=None,
        metavar="EPIC_URL",
        help=(
            "also run the parallel EPIC scheduler's checks: the coordination root's "
            "filesystem and the scheduler root's permissions, and whether gh can read this "
            "EPIC's sub-issues and dependencies (read-only; nothing is created)"
        ),
    )

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
    _add_accept_config_change(s)

    re_ = sub.add_parser("resume", help="continue a persisted run")
    re_.add_argument("--dry-run", action="store_true")
    re_.add_argument(
        "--max-steps", type=_positive_int, default=50, help="steps for this invocation (>= 1)"
    )
    re_.add_argument("--allow-merge", action="store_true")
    re_.add_argument("--full-prompt", action="store_true")
    _add_accept_config_change(re_)

    ub = sub.add_parser(
        "unblock",
        help=(
            "leave BLOCKED explicitly: re-inspect GitHub, re-enter a phase or refuse; then 'resume'"
        ),
        description=(
            "Requires a BLOCKED run. The controller re-runs its recovery inspection "
            "against live GitHub and either re-enters the one phase that inspection "
            "supports (through the same transition validation as any other step) or "
            "refuses and stays BLOCKED. No agent runs; the operator action is recorded "
            "in state and in the run log."
        ),
    )
    ub.add_argument(
        "--reason",
        required=True,
        help="why the operator is unblocking (recorded in state and in the run log)",
    )
    ub.add_argument(
        "--dry-run",
        action="store_true",
        help="read GitHub and report the decision without writing state or the log",
    )
    _add_accept_config_change(ub)
    action = ub.add_mutually_exclusive_group()
    action.add_argument(
        "--approve-action",
        default="",
        metavar="ACTION_ID",
        help=(
            "approve the acceptance action a FIX plan awaits (its id from 'autoforge status'); "
            "'resume' then publishes exactly its payload on the run's issue"
        ),
    )
    action.add_argument(
        "--decline-action",
        default="",
        metavar="ACTION_ID",
        help=(
            "decline the acceptance action a FIX plan awaits: the unsent plan is dropped and "
            "FIX launches the fixer again with the decline in its prompt"
        ),
    )

    st = sub.add_parser("status", help="show persisted run status")
    shown = st.add_mutually_exclusive_group()
    shown.add_argument("--json", action="store_true", dest="as_json")
    shown.add_argument(
        "--action",
        default="",
        metavar="ACTION_ID",
        help=(
            "print the exact payload of a pending acceptance action and nothing else "
            "(exit 1 when no such action is persisted)"
        ),
    )

    _add_local_parser(sub)
    _add_epic_parser(sub)
    return p


def _add_accept_config_change(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--accept-config-change",
        action="store_true",
        help=(
            "continue a REMOTE run although this invocation's profile routing or agent "
            "worktree root differs from the configuration the run was recorded under: the "
            "change is written to the run log and becomes the run's configuration (#219)"
        ),
    )


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


def _add_epic_parser(sub) -> None:
    """`autoforge epic ...`: the parallel EPIC scheduler (ADR 0008, #183).

    ``epic _worker`` is matched in :func:`main` before this parser, so it is
    neither a choice here nor in ``--help``.
    """
    ep = sub.add_parser(
        "epic",
        help="parallel EPIC scheduler: run, resume, status, stop, archive (REMOTE only)",
        description=(
            "Run up to --max-parallel issues of one EPIC at once, each in its own scheduler "
            "namespace and worktree, admitted in dependency order from GitHub's native "
            "sub-issues and blocked-by links. Every worker stops at its settlement boundary "
            "(READY_FOR_MERGE, BLOCKED, DONE, FAILED); nothing is merged. REMOTE only; "
            "--state-dir is refused, and so is an EPIC of another repository than this "
            "checkout's 'origin' remote, before anything is read or written."
        ),
    )
    esub = ep.add_subparsers(dest="epic_command", required=True)

    er = esub.add_parser(
        "run",
        help="start a scheduler for an EPIC (refused while a claim of it is open)",
        description=(
            "Start a scheduler invocation: it holds the controller lock for its whole life, "
            "admits ready issues up to --max-parallel and exits when no worker runs and "
            "nothing is admissible. Refused while any claim of this EPIC is open: an earlier "
            "scheduler ended without closing it, and 'epic resume' classifies it."
        ),
    )
    er.add_argument("epic", metavar="EPIC_URL", help="the EPIC issue URL")
    _add_max_parallel(er)
    er.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "print the snapshot, the ordered verdicts and the admission plan; no lock, "
            "namespace, claim, worktree or worker"
        ),
    )
    er.add_argument(
        "--force",
        action="store_true",
        help=(
            "move an unreadable scheduler.json aside (scheduler.json.corrupt-<timestamp>); "
            "it does nothing else: it never closes a claim or moves a state"
        ),
    )

    eres = esub.add_parser(
        "resume",
        help="classify an EPIC's namespaces and claims, then continue like 'epic run'",
        description=(
            "Reconcile every namespace of the EPIC first (a settled one's open claim is "
            "closed, a resumable one is admitted as a resume, an uncertain one is held), "
            "then admit like 'epic run'. Refused (exit 2) while a worker of an earlier "
            "invocation still runs: it keeps the controller lock until it exits."
        ),
    )
    eres.add_argument("epic", metavar="EPIC_URL", help="the EPIC issue URL")
    _add_max_parallel(eres)
    _add_accept_config_change(eres)

    est = esub.add_parser(
        "status",
        help="every child's category and next step (lock-free, read-only)",
        description=(
            "Take no lock and change nothing: the snapshot from GitHub, and each child's "
            "namespace, claim and run lock read lock-free. Each child gets exactly one "
            "category; a claim's worker PID is shown as a diagnostic and never signalled."
        ),
    )
    est.add_argument("epic", metavar="EPIC_URL", help="the EPIC issue URL")
    est.add_argument(
        "--json",
        action="store_true",
        dest="as_json",
        help="print one autoforge.epic-status/1 document (docs/usage.md)",
    )

    esp = esub.add_parser(
        "stop",
        help="ask the EPIC's scheduler to stop (lock-free; default: drain)",
        description=(
            "Publish a stop request for the invocation scheduler.json names. A drain "
            "closes admission and lets every worker return at its next step boundary; "
            f"--now behaves like Ctrl-C in the scheduler's terminal. Note: {DRAIN_HELP}. "
            "With no live scheduler, surviving workers drain at their next step boundary, "
            "and --now cannot signal them. Refused when no scheduler.json exists; nothing "
            "is ever removed."
        ),
    )
    esp.add_argument("epic", metavar="EPIC_URL", help="the EPIC issue URL")
    esp.add_argument(
        "--now",
        action="store_true",
        help="interrupt the workers, as Ctrl-C in the scheduler's terminal does",
    )

    ea = esub.add_parser(
        "archive",
        help="release one settled or uncertain issue for a fresh run (never automatic)",
        description=(
            "Under the controller lock and the issue's run lock: close an open claim as "
            "archived (an unusable claim file is moved aside), then move state/state.json "
            "aside as state.json.archived-<timestamp>. The logs, the worktree, the binding "
            "and the claim history are kept; the run's loop-bound evidence then lives only "
            "in the archived state. Refused while the issue has a live owner. Re-running it "
            "completes an interrupted archive."
        ),
    )
    ea.add_argument("epic", metavar="EPIC_URL", help="the EPIC issue URL")
    ea.add_argument("issue", type=_positive_int, metavar="ISSUE", help="the issue number")


def _add_max_parallel(parser: argparse.ArgumentParser) -> None:
    low, high = MAX_PARALLEL_BOUNDS
    parser.add_argument(
        "--max-parallel",
        type=_max_parallel,
        default=None,
        metavar="N",
        help=(
            f"workers at once, {low} to {high} (default: scheduler.max_parallel in the "
            "configuration)"
        ),
    )


def _load_cfg(args) -> AutoForgeConfig:
    return load_config_file(args.config)


def main(argv: list[str] | None = None) -> int:
    words = sys.argv[1:] if argv is None else list(argv)
    if words[:2] == ["epic", "_worker"]:
        # The scheduler's hidden worker entry point (ADR 0008 D9.1, #182): not
        # a command an operator types, so it is not in the parser or --help.
        from .worker import run_worker

        return run_worker(words[2:])
    parser = build_parser()
    args = parser.parse_args(words)
    try:
        if args.command == "doctor":
            return cmd_doctor(args)
        if args.command == "run":
            return cmd_run(args)
        if args.command == "step":
            return cmd_step(args)
        if args.command == "resume":
            return cmd_resume(args)
        if args.command == "unblock":
            return cmd_unblock(args)
        if args.command == "status":
            return cmd_status(args)
        if args.command == "epic":
            return cmd_epic(args)
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
def _global_flags(args) -> tuple[str, ...]:
    """``--config`` and ``--state-dir`` as this process was given them, and nothing else.

    An agent failure's ``resume`` line repeats them (#189): the CLI has no
    configuration discovery, so a bare ``resume`` would find no run or run on
    the built-in defaults. They are paths, as typed; no configuration value.
    """
    flags: list[str] = []
    if args.config:
        flags += ["--config", args.config]
    if args.state_dir:
        flags += ["--state-dir", args.state_dir]
    return tuple(flags)


def _engine_for(args, cfg: AutoForgeConfig | None = None) -> ControllerEngine:
    cfg = cfg or _load_cfg(args)
    state_dir = args.state_dir or cfg.state_dir
    engine = ControllerEngine(config=cfg, state_dir=state_dir)
    engine.cli_global_flags = _global_flags(args)
    engine.config_file = os.path.abspath(args.config) if args.config else ""
    return engine


def _check_configuration(engine: ControllerEngine, args, *words: str, dry_run: bool) -> None:
    """The recorded-configuration gate of ``step``, ``resume`` and ``unblock`` (#219).

    Refuses (``ConfigurationDriftError``, exit 2) before any phase executes
    when this invocation's configuration differs from the run's, unless
    ``--accept-config-change``; prints the engine's notice on stderr.
    """
    notice = engine.check_run_configuration(
        words, accept_change=bool(getattr(args, "accept_config_change", False)), dry_run=dry_run
    )
    if notice:
        print(redact(f"autoforge: {notice}"), file=sys.stderr)


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


def _guard_scheduler_ownership(
    engine: ControllerEngine, state: AutoForgeState, command: str
) -> None:
    """ADR 0008 D17.2's forward guard, under L0 and before anything is written.

    Refuses (``SchedulerOwnedIssueError``, exit 2) when a scheduler
    namespace owns the issue ``command`` would act on, the run's
    ``current_issue_url``, then hands the engine the same predicate, which
    ``_verify_issue_selectable`` applies to every issue it is asked about
    (``next_issue_url`` included). ``unblock`` of a namespace's own state
    directory exempts that namespace. LOCAL runs name no issue and are not
    checked.
    """
    if state.mode is WorkflowMode.LOCAL:
        return
    root = engine.coordination_root()
    exempt = (
        namespace_issue_of_state_dir(engine.paths.state_dir, root) if command == "unblock" else None
    )
    refuse_scheduler_owned_issue(root, state.current_issue_url, command, exempt_issue=exempt)
    engine.issue_ownership = standalone_ownership_predicate(root, exempt_issue=exempt)


def _bind_standalone_run(engine: ControllerEngine, args, command: str) -> None:
    """:func:`_bind_existing_run` for a standalone ``step`` or ``resume`` (ADR 0008 D17.3).

    The state directory this invocation names (``--state-dir`` or
    ``config.state_dir``) is judged before the binding looks for a run in it
    or in LOCAL's location: one inside the scheduler root that holds no
    ``state.json`` would otherwise give way to a LOCAL run's, and the command
    would continue that run instead of refusing. The directory bound is
    judged again. Both before the lock and before any state is read; a dry
    run is judged by neither (:meth:`ControllerEngine.refuse_scheduler_state_dir`).
    """
    if not args.dry_run:
        engine.refuse_scheduler_state_dir(command)
    if args.state_dir:
        return
    _bind_existing_run(engine)
    if not args.dry_run:
        engine.refuse_scheduler_state_dir(command)


def _doctor_runner():
    """Command runner used by `doctor` (indirection so tests can inject a fake)."""
    return None


def cmd_doctor(args) -> int:
    results = run_doctor(
        config_path=args.config,
        state_dir=args.state_dir,
        runner=_doctor_runner(),
        epic_url=args.epic,
    )
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
            {
                "name": r.name,
                "ok": r.ok,
                "required": r.required,
                "skipped": r.skipped,
                "detail": redact(r.detail),
            }
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
    # Keep stdout to the JSON document alone in --json mode.
    print("All checks passed.", file=sys.stderr if as_json else sys.stdout)
    return 0


def _progress_line(line: str) -> None:
    """One live progress line (already bounded and redacted), on stderr at once."""
    print(line, file=sys.stderr, flush=True)


def _show_progress(engine: ControllerEngine) -> None:
    """Stream a real run's agent progress to stderr (#192).

    Only the non-dry paths call this: a dry run launches nothing. stdout is
    left to the step outcomes, so nothing about progress reaches output a
    script reads.
    """
    engine.progress_output = _progress_line


def _print_outcome_now(o: StepOutcome) -> None:
    """Print one step's outcome as soon as the step finished, not after the loop."""
    print_step_outcome(o)
    sys.stdout.flush()


def _finish(
    engine: ControllerEngine,
    outcomes: list[StepOutcome],
    allow_merge: bool,
    *,
    printed: bool = False,
) -> int:
    """Print the outcomes (unless ``printed`` already, step by step) and the run's end."""
    if not printed:
        for o in outcomes:
            print_step_outcome(o)
    state = engine.state
    assert state is not None
    if state.phase == Phase.READY_FOR_MERGE:
        print_ready_banner(
            state,
            gate_open=engine.merge_gate_open(allow_merge),
            certificate=_review_certificate_check(engine, state),
        )
        return 0
    if state.phase in (Phase.BLOCKED, Phase.FAILED):
        # The whole line is state-derived (`run_id` is a validated path
        # component, not a checked-for-secrets value), so the line crosses
        # the redaction boundary assembled, not field by field.
        print(redact(f"\nrun {state.run_id} is {state.phase.value}: {state.block_reason or '-'}"))
        return 1
    return 0


def _existing_run_guard(
    engine: ControllerEngine, force: bool, command: str = "run"
) -> tuple[int | None, bool]:
    """Decide whether a fresh run may overwrite the run at ``engine.paths``.

    Returns ``(exit_code_or_None, corrupt)``. Must be called with the
    controller lock held: a verdict taken before the lock could go stale.
    The inspection goes through the engine's held state root -- the same
    capability the first save and every later write will use -- so the
    directory that is inspected is the directory that is written.

    ``command`` is the subcommand the operator actually typed, so the advice
    names a command that can be copied (`run --force`, `local run --force`)
    rather than a bare flag.
    """
    paths = engine.paths
    try:
        existing = engine.existing_run()
    except StateError as exc:
        # Unreadable / foreign-protocol state is fatal: a fresh run must
        # never silently replace it (merge counters etc. would be lost).
        if not force:
            # The error quotes the field it could not read (an unknown phase
            # is echoed verbatim), so the refusal is state-derived output
            # like the terminal lines and crosses the boundary assembled.
            print(
                redact(
                    f"autoforge: error: {exc}\n"
                    "autoforge: error: refusing to start a new run over an unreadable "
                    f"state file — repair it, or use '{command} --force' to move it "
                    f"aside as {paths.state_file.name}.corrupt-<timestamp> and start over"
                ),
                file=sys.stderr,
            )
            return 2, False
        return None, True
    if existing is None:
        return None, False
    if existing.phase not in TERMINAL_PHASES and not force:
        print(
            redact(
                f"autoforge: error: existing run {existing.run_id} "
                f"in phase {existing.phase.value} — use 'resume' to continue "
                f"or '{command} --force' to discard it"
            ),
            file=sys.stderr,
        )
        return 2, False
    return None, False


def cmd_run(args) -> int:
    cfg = _load_cfg(args)
    engine = _engine_for(args, cfg)
    engine.validate_config()

    if args.dry_run:
        # Fully side-effect-free: in-memory state, plan printed, nothing written.
        engine.new_run(args.epic, args.issue)
        outcomes = engine.run(max_steps=args.max_steps, dry_run=True, allow_merge=args.allow_merge)
        for o in outcomes:
            assert o.plan is not None
            print_plan(o.plan, full_prompt=args.full_prompt)
        return 0

    # A state directory inside the scheduler root is a namespace's, never a
    # standalone run's (ADR 0008 D17.3); refused before the lock is taken.
    engine.refuse_scheduler_state_dir("run")
    # Inspect, decide, quarantine, write the first state AND execute under
    # one continuous lock: a verdict taken before the lock could go stale
    # (another controller may have repaired, replaced or created state.json
    # in the meantime) and would then quarantine or overwrite a perfectly
    # valid run; releasing the lock between the first save and the execution
    # would let a second controller take over the repository and both would
    # then run agents and persist state over each other.
    with engine.locked():
        rc, corrupt = _existing_run_guard(engine, args.force)
        if rc is not None:
            return rc
        engine.new_run(args.epic, args.issue)
        assert engine.state is not None
        # A scheduler-owned issue is refused before anything is written
        # (ADR 0008 D17.2): no quarantine, no state, no run log.
        _guard_scheduler_ownership(engine, engine.state, "run")
        # What later commands of the run are checked against (#219).
        engine.record_run_configuration()
        if corrupt:
            moved = engine.quarantine_state()
            print(f"autoforge: moved unreadable state file aside: {moved}", file=sys.stderr)
        engine.save()
        _show_progress(engine)
        outcomes = engine.run(
            max_steps=args.max_steps,
            dry_run=False,
            allow_merge=args.allow_merge,
            on_outcome=_print_outcome_now,
        )
    return _finish(engine, outcomes, args.allow_merge, printed=True)


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

    engine.refuse_scheduler_state_dir("local run")
    # One continuous lock over inspect -> decide -> first save -> execute,
    # for the same reasons as `cmd_run`.
    with engine.locked():
        rc, corrupt = _existing_run_guard(engine, args.force, "local run")
        if rc is not None:
            return rc
        engine.new_local_run(args.feature, allow_dirty=args.allow_dirty)
        engine.validate_config()
        assert engine.state is not None
        if corrupt:
            moved = engine.quarantine_state()
            print(f"autoforge: moved unreadable state file aside: {moved}", file=sys.stderr)
        engine.save()
        _show_progress(engine)
        outcomes = engine.run(
            max_steps=args.max_steps, dry_run=False, on_outcome=_print_outcome_now
        )
    return _finish(engine, outcomes, allow_merge=False, printed=True)


def cmd_step(args) -> int:
    engine = _engine_for(args)
    _bind_standalone_run(engine, args, "step")
    if args.dry_run:
        # Read-only: no lock, nothing written.
        state = engine.load()
        if state.phase not in TERMINAL_PHASES:
            _check_configuration(engine, args, "step", dry_run=True)
        outcome = engine.step(dry_run=True, allow_merge=args.allow_merge)
        if outcome.plan is not None:
            print_plan(outcome.plan, full_prompt=args.full_prompt)
        return 0
    # Load and execute under one lock: a snapshot loaded before the lock
    # could already have been replaced by another controller.
    with engine.locked():
        state = engine.load()
        _guard_scheduler_ownership(engine, state, "step")
        if state.phase not in TERMINAL_PHASES:
            _check_configuration(engine, args, "step", dry_run=False)
        _show_progress(engine)
        outcome = engine.step(dry_run=False, allow_merge=args.allow_merge)
    return _finish(engine, [outcome], args.allow_merge)


def cmd_resume(args) -> int:
    engine = _engine_for(args)
    _bind_standalone_run(engine, args, "resume")
    if args.dry_run:
        # Read-only: no lock, nothing written.
        state = engine.load()
        rc = _resume_holding_state(engine, state, args.allow_merge)
        if rc is not None:
            return rc
        _check_configuration(engine, args, "resume", dry_run=True)
        outcomes = engine.run(max_steps=args.max_steps, dry_run=True, allow_merge=args.allow_merge)
        for o in outcomes:
            assert o.plan is not None
            print_plan(o.plan, full_prompt=args.full_prompt)
        return 0
    # Load, decide and execute under one lock: a snapshot loaded before the
    # lock could already have been replaced by another controller.
    with engine.locked():
        state = engine.load()
        _guard_scheduler_ownership(engine, state, "resume")
        rc = _resume_holding_state(engine, state, args.allow_merge)
        if rc is not None:
            return rc
        _check_configuration(engine, args, "resume", dry_run=False)
        _show_progress(engine)
        outcomes = engine.run(
            max_steps=args.max_steps,
            dry_run=False,
            allow_merge=args.allow_merge,
            on_outcome=_print_outcome_now,
        )
    return _finish(engine, outcomes, args.allow_merge, printed=True)


def cmd_unblock(args) -> int:
    """``autoforge unblock --reason ...``: exit 0 when a phase was re-entered.

    The decision is the engine's (:meth:`ControllerEngine.unblock`): a
    refusal leaves the run BLOCKED, prints why, and exits 1; a run that is
    not BLOCKED is a StateTransitionError (exit 2). The run is not resumed
    here: the operator reviews the outcome and runs ``resume``.
    """
    engine = _engine_for(args)
    if not args.state_dir:
        _bind_existing_run(engine)
    approve = str(getattr(args, "approve_action", "") or "")
    decline = str(getattr(args, "decline_action", "") or "")
    # The words a configuration refusal prints for this command (#219).
    words = ("unblock", "--reason", "<why>")
    if args.dry_run:
        # Reads GitHub; writes nothing (no state, no run log, no lock).
        state = engine.load()
        if state.phase == Phase.BLOCKED:
            _check_configuration(engine, args, *words, dry_run=True)
        outcome = engine.unblock(
            args.reason, dry_run=True, approve_action=approve, decline_action=decline
        )
        print_unblock_outcome(outcome)
        return 0
    # Load, decide and write under one lock, like `step`. An accepted
    # configuration change is its own recorded write, made before the
    # decision: a refused unblock leaves it in place.
    with engine.locked():
        state = engine.load()
        _guard_scheduler_ownership(engine, state, "unblock")
        if state.phase == Phase.BLOCKED:
            _check_configuration(engine, args, *words, dry_run=False)
        outcome = engine.unblock(args.reason, approve_action=approve, decline_action=decline)
    print_unblock_outcome(outcome)
    if not outcome.unblocked:
        return 1
    if engine.state_dir_in_scheduler_root():
        # A scheduler namespace's run (ADR 0008 D17.3): a standalone
        # 'resume' would refuse this state directory.
        print(
            "continue with the scheduler: 'autoforge epic resume' "
            "(a standalone 'resume' refuses this state directory)"
        )
        return 0
    # With the flags this command was given, as a block reason's 'resume'
    # line does (#189): a bare 'resume' would find no run or other defaults.
    print(redact(f"continue with: {engine.resume_command()}"))
    return 0


def print_unblock_outcome(o: UnblockOutcome) -> None:
    # State-derived and operator-supplied text; redacted whole like a step outcome.
    print(redact(f"[{o.run_id}] {o.message}"))


def _resume_holding_state(
    engine: ControllerEngine, state: AutoForgeState, allow_merge: bool
) -> int | None:
    """Exit code when ``resume`` has nothing to execute; None when it does."""
    if state.phase == Phase.READY_FOR_MERGE and not engine.merge_gate_open(allow_merge):
        # Holding state: nothing runs unless the merge gate is open. With the
        # gate open, engine.run() performs the controller-side pre-merge
        # verification instead (bounded re-checks of inconclusive GitHub data
        # happen there, one attempt per resume), the review certificate's
        # live check included (#175); the banner says only whether it
        # applies against state and the configuration in force.
        print_ready_banner(state, certificate=_review_certificate_check(engine, state))
        return 0
    if state.phase in TERMINAL_PHASES:
        if state.phase == Phase.DONE:
            print(redact(f"workflow already DONE (run {state.run_id}) — nothing to do"))
            return 0
        how = (
            "and run 'autoforge unblock --reason ...' to have the controller re-inspect "
            "GitHub and re-enter a phase, or start a new run"
            if state.phase == Phase.BLOCKED and state.mode != WorkflowMode.LOCAL
            else "and start a new run"
        )
        print(
            redact(
                f"run {state.run_id} is in terminal phase {state.phase.value}: "
                f"{state.block_reason or '-'} — inspect {engine.paths.logs_dir}/ {how}"
            )
        )
        return 1
    return None


def _load_replan_journal(state: AutoForgeState) -> ReplanTransaction | None:
    """Read the persisted replan journal the way the engine reads it.

    ``None`` when the run has no journal. A journal whose fields cannot be
    read comes back ``REJECTED`` with ``journal_defects`` naming them, for
    status to describe rather than die on. The one shape ``from_dict``
    refuses outright -- a journal that is not a JSON object -- never reaches
    here: ``AutoForgeState.from_dict`` rejects it, so ``load()`` fails both
    output modes at the state boundary (exit 2) before anything is printed.
    """
    if not state.replan_transaction:
        return None
    return ReplanTransaction.from_dict(state.replan_transaction)


def _replan_journal_summary(txn: ReplanTransaction | None) -> dict | None:
    """The derived ``replan_journal`` field of ``status --json``.

    ``status --json`` prints ``replan_transaction`` as persisted, so the
    forensic evidence of a corrupt journal is never rewritten -- which leaves
    a consumer no way to tell a usable journal from a corrupt one without
    reimplementing the validation. This sibling field answers that from the
    same ``ReplanTransaction`` the human ``status`` prints its ``stage`` and
    ``journal`` lines from, so the two outputs cannot disagree. It is never
    merged into the raw journal.
    """
    if txn is None:
        return None
    return {
        "readable": not txn.journal_defects,
        "stage": txn.stage.value,
        "defects": list(txn.journal_defects),
    }


# -- epic commands (ADR 0008, #183) ----------------------------------------------


def _epic_graph(cfg: AutoForgeConfig) -> GraphReader:
    """The GitHub reads the ``epic`` commands make (indirection so tests can inject a fake)."""
    return GitHubClient(gh_command=cfg.github.command, timeout_seconds=cfg.github.timeout_seconds)


def _epic_scheduler_options() -> dict:
    """Extra :class:`SchedulerInputs` for ``epic run``/``resume`` (a test's injection point)."""
    return {}


def _epic_flags(args) -> tuple[str, ...]:
    """What every command an ``epic`` command prints repeats: ``--config``, as typed."""
    return ("--config", args.config) if args.config else ()


def _say(text: str) -> None:
    print(redact(f"autoforge epic: {text}"), file=sys.stderr, flush=True)


def cmd_epic(args) -> int:
    if args.state_dir:
        raise ConfigurationError(
            "'epic' commands refuse --state-dir: each issue's state lives in its own "
            "scheduler namespace, which 'autoforge epic status <epic>' names"
        )
    # Before anything is read or written here: an EPIC of another repository
    # than this checkout's would bind its issues in this checkout's namespaces,
    # or stop or archive this checkout's EPIC of the same number.
    checkout_epic(os.getcwd(), args.epic)
    if args.epic_command == "run":
        return cmd_epic_run(args, resume=False)
    if args.epic_command == "resume":
        return cmd_epic_run(args, resume=True)
    if args.epic_command == "status":
        return cmd_epic_status(args)
    if args.epic_command == "stop":
        return cmd_epic_stop(args)
    if args.epic_command == "archive":
        return cmd_epic_archive(args)
    raise ConfigurationError(f"unknown epic command {args.epic_command}")


def cmd_epic_run(args, *, resume: bool) -> int:
    """``epic run`` and ``epic resume``: one scheduler invocation (#182) and its exit status.

    The scheduler makes no run/resume split; ``epic run``'s refusal of open
    claims and its ``--force`` run inside it, holding L0, before anything is
    written. A run that ends with nothing admissible while claims of the
    EPIC are still open exits :data:`~autoforge.epic_commands.EXIT_OPEN_CLAIMS`.
    """
    cfg = _load_cfg(args)
    flags = _epic_flags(args)
    epic_url = parse_issue_url(args.epic).canonical
    graph = _epic_graph(cfg)
    repository = os.getcwd()
    standalone = os.path.abspath(cfg.state_dir)
    if not resume and args.dry_run:
        plan = dry_run_plan(
            graph,
            repository_root=repository,
            epic_url=epic_url,
            max_parallel=args.max_parallel or cfg.scheduler.max_parallel,
            standalone_state_dir=standalone,
            force=args.force,
            flags=flags,
        )
        for line in plan.lines:
            print(line)
        return plan.exit_code
    inputs = SchedulerInputs(
        repository=repository,
        epic_url=epic_url,
        config=cfg,
        graph=graph,
        config_path=os.path.abspath(args.config) if args.config else "",
        max_parallel=args.max_parallel,
        accept_config_change=resume and args.accept_config_change,
        standalone_state_dir=standalone,
        **_epic_scheduler_options(),
    )
    scheduler = EpicScheduler(inputs)
    if not resume:
        inputs.before_start = epic_run_guard(
            force=args.force, flags=flags, epic_url=epic_url, say=scheduler.say
        )
    code = scheduler.run()
    if code != 0:
        return code
    left = claims_left_open(scheduler.coordination_root, scheduler.epic_ref)
    if not left:
        return code
    for line in left_open_lines(left, flags, epic_url):
        scheduler.say(line)
    return EXIT_OPEN_CLAIMS


def cmd_epic_status(args) -> int:
    cfg = _load_cfg(args)
    report = collect_status(
        _epic_graph(cfg),
        repository_root=os.getcwd(),
        epic_url=args.epic,
        standalone_state_dir=os.path.abspath(cfg.state_dir),
        flags=_epic_flags(args),
    )
    if args.as_json:
        print(json.dumps(report.to_dict(), indent=2))
    else:
        for line in report.lines():
            print(line)
    return report.exit_code


def _epic_repository_id(args) -> Callable[[str], int]:
    """GitHub's numeric id of a repository, as ``epic stop`` and ``epic archive`` read it.

    Called once a ``scheduler.json`` or a binding is found to compare with,
    so the configuration is loaded then.
    """

    def read(repository: str) -> int:
        return _epic_graph(_load_cfg(args)).get_repository_id(repository)

    return read


def cmd_epic_stop(args) -> int:
    """``epic stop``: lock-free; GitHub is read for the repository id ``scheduler.json`` records."""
    epic = parse_issue_url(args.epic)
    mode = StopMode.NOW if args.now else StopMode.DRAIN
    outcome = stop_scheduler(
        coordination_root_of(os.getcwd()), epic, mode, _epic_repository_id(args)
    )
    for line in outcome.lines:
        print(line)
    return outcome.exit_code


def cmd_epic_archive(args) -> int:
    """``epic archive``: under L0 (a held one is exit 2); GitHub is read for the binding's id."""
    epic = parse_issue_url(args.epic)
    lock_path = repository_lock_path(os.getcwd())
    with ControllerLock(lock_path):
        report = archive_issue(
            lock_path.parent.parent, epic.canonical, args.issue, _epic_repository_id(args)
        )
    for line in report.lines():
        print(line)
    return 0


def cmd_status(args) -> int:
    engine = _engine_for(args)
    if not args.state_dir:
        _bind_existing_run(engine)
    state = engine.load()  # StateError when missing/corrupt
    if getattr(args, "action", ""):
        return _print_action_payload(state, args.action)
    # One load of the journal feeds every output mode, so what `--json`
    # derives and what the human summary prints cannot disagree.
    txn = _load_replan_journal(state)
    # Everything status shows is state-derived, and state carries text the
    # agents controlled (`block_reason`, findings, a hand-edited journal), so
    # both output modes cross the same redaction boundary as the run log.
    certificate = _review_certificate_check(engine, state)
    if args.as_json:
        # to_dict() already carries "mode" plus the local fields, so the JSON
        # shape stays additive for remote consumers; `replan_journal` and
        # `acceptance_actions` are derived beside the raw fields, never
        # merged into them.
        document = {
            **state.to_dict(),
            "replan_journal": _replan_journal_summary(txn),
            "acceptance_actions": _acceptance_actions(state),
            "review_certificate_status": _review_certificate_status(state, certificate),
            "outer_sandbox": {
                "mode": engine.config.sandbox.mode,
                "summary": sandbox_mode_summary(
                    engine.config.sandbox, state.mode == WorkflowMode.LOCAL
                ),
            },
        }
        print(json.dumps(redact_dict(document), indent=2, sort_keys=True))
        return 0
    evidence = _render_outer_sandbox(state, engine.config) + _render_validation_evidence(
        state, engine.config, certificate
    )
    if state.mode == WorkflowMode.LOCAL:
        print(redact(_render_local_status(state, txn, evidence)))
        return 0
    print(redact(_render_status(state, txn, _render_acceptance_actions(state, engine), evidence)))
    return 0


# How many records of the current candidate ``status`` lists, newest last.
_STATUS_RECORDS = 16
_STATUS_TAIL_CHARS = 200


def _render_outer_sandbox(state: AutoForgeState, config: AutoForgeConfig) -> list[str]:
    """``sandbox.mode`` for ``status`` (ADR 0005 D12.4): configuration only, no probe.

    A mode that may fall back says so; a ``disabled`` run reads "logical
    isolation only". Whether this host can enforce is ``doctor``'s to show.
    """
    summary = sandbox_mode_summary(config.sandbox, state.mode == WorkflowMode.LOCAL)
    return ["", "Outer sandbox:", f"  {summary}"]


def _review_certificate_check(
    engine: ControllerEngine, state: AutoForgeState
) -> tuple[bool | None, str] | None:
    """Whether the held review certificate applies now (ADR 0006 D12.3), for ``status``.

    ``None`` when none is held; otherwise ``(applicable, reason)``, with
    ``applicable`` ``None`` and the reason saying why when the check could
    not run (a LOCAL contract that no longer matches this invocation, say).
    Reads the configuration in force, never GitHub: the live comparison is
    the merge gate's.
    """
    if not state.review_certificate:
        return None
    try:
        problem = engine.review_certificate_problem()
    except AutoForgeError as exc:
        return None, f"not checked: {exc}"
    return not problem, problem


def _review_certificate_status(
    state: AutoForgeState, check: tuple[bool | None, str] | None
) -> dict | None:
    """The derived ``review_certificate_status`` of ``status --json`` (#174); ``None`` for none.

    Beside the raw ``review_certificate``, never merged into it: the fields
    a consumer reads (kind, candidate, round, set and policy digests,
    ``issued_at``), its applicability against state and the configuration
    in force with the reason when it does not apply, and the fixed note that
    it is no merge permission.
    """
    certificate = state.review_certificate
    if not certificate or check is None:
        return None
    applicable, problem = check
    return {
        "kind": certificate["kind"],
        "candidate": certificate["candidate"],
        "review_round": certificate["review_round"],
        "validation_set_digest": certificate["validation_set_digest"],
        "policy_digest": certificate["policy_digest"],
        "issued_at": certificate["issued_at"],
        "applicable": applicable,
        "problem": problem,
        "note": NOT_A_PERMISSION,
    }


def _render_validation_evidence(
    state: AutoForgeState,
    config: AutoForgeConfig,
    certificate: tuple[bool | None, str] | None = None,
) -> list[str]:
    """Candidate validation evidence for ``status`` (ADR 0006 D8.6; the caller redacts).

    The evidence policy (the configured set's size and the mode records are
    made under, and under ``disabled`` that validators run unsandboxed), a
    pending attempt marker, the override watermark, a producer's validation
    failure counter, whether a binding is held, the review certificate (what
    it names, whether it applies, as ``certificate`` says, and that it is no
    merge permission), and the records of the current candidate: the REMOTE
    current HEAD's or the LOCAL bound workspace's, else the newest record's.
    Read-only: a marker is reported, never converted.
    """
    local = state.mode == WorkflowMode.LOCAL
    if local:
        key = "local.validation_commands (the run contract)"
        count = len(state.local_contract().validation_commands)
    else:
        key = "validation.commands"
        count = len(config.validation.commands)
    mode = config.sandbox.mode
    policy = f"{count} definition(s) in {key}; records are made under sandbox.mode {mode}"
    if not config.sandbox.enabled:
        policy += (
            ", so validators run unsandboxed as the operator's UID and their records say "
            "enforced: false"
        )
    elif local and mode == "preferred":
        policy += (
            ", so on a host that cannot enforce the outer sandbox validators run unsandboxed "
            "as the operator's UID and their records say enforced: false"
        )
    lines = ["", "Validation evidence:", f"  Policy:  {policy}"]
    records = state.validation_evidence()
    if state.validation_attempt:
        marker = parse_marker(state.validation_attempt)
        lines.append(
            f"  Pending attempt: definition [{marker.index}] {' '.join(marker.argv)!r} for "
            f"{marker.candidate.describe()}, marked {state.validation_attempt['marked_at']} "
            "(the next step or resume records it INTERRUPTED and runs it again)"
        )
    if state.validation_override_seq is not None:
        lines.append(f"  Override watermark: seq {state.validation_override_seq}")
    counter = state.producer_validation_failure
    if counter:
        lines.append(
            f"  {counter['phase']} validation failures: {counter['count']} of "
            f"{MAX_FAILURE_COUNT} (last: record #{counter['last_record_seq']}, commit "
            f"{counter['candidate_commit'][:12]})"
        )
    if state.review_evidence:
        lines.append(
            f"  REVIEW evidence: round {state.review_evidence['review_round']}, "
            f"{len(state.review_evidence['records'])} record(s)"
        )
    if state.review_certificate:
        lines.append(f"  Review certificate: {describe_certificate(state.review_certificate)}")
        if certificate is None:
            applies = "not checked"
        else:
            applicable, problem = certificate
            applies = "yes" if applicable else problem if applicable is None else f"no: {problem}"
        lines.append(f"    Applicable: {applies}")
        lines.append(f"    Note: {NOT_A_PERMISSION}")
    if not records:
        lines.append("  Records: none")
        return lines
    current = records[-1].candidate
    for view in reversed(records):
        candidate = view.candidate
        if (
            isinstance(candidate, RemoteCandidate) and candidate.commit == state.current_head_sha
        ) or (
            isinstance(candidate, LocalCandidate)
            and candidate.fingerprint == state.workspace_fingerprint
        ):
            current = candidate
            break
    mine = [r for r in records if r.candidate.identity == current.identity]
    shown = mine[-_STATUS_RECORDS:]
    lines.append(
        f"  Records for {current.describe()}: {len(mine)} of {len(records)} retained"
        + (f" (the newest {len(shown)} shown)" if len(shown) < len(mine) else "")
    )
    for view in shown:
        lines.append(f"    {record_summary(view)}")
        if view.outcome in (Outcome.FAIL, Outcome.TIMEOUT):
            output = view.raw["output"]
            tail = " ".join((output["stderr_tail"] or output["stdout_tail"]).split())
            if tail:
                lines.append(f"      output tail: {tail[-_STATUS_TAIL_CHARS:]}")
    return lines


def _acceptance_records(state: AutoForgeState) -> list[EffectRecord]:
    """The persisted K9 records of the current plan (#211); none for a LOCAL run."""
    if state.mode == WorkflowMode.LOCAL:
        return []
    return [r for r in state.phase_effects().records if r.kind is EffectKind.ACCEPTANCE_COMMENT]


def _print_action_payload(state: AutoForgeState, action_id: str) -> int:
    """``status --action``: the exact payload bytes of a persisted acceptance action (#211).

    Exactly the body the controller would publish (and an operator may post
    instead), with nothing before or after it; it is redaction-invariant by
    the record's validation, so nothing is rewritten here. An id no
    persisted record has (a declined or stale action keeps no payload), or a
    LOCAL run, exits 1 and prints nothing.
    """
    for record in _acceptance_records(state):
        if record.action_id == action_id.strip():
            sys.stdout.write(record.body)
            sys.stdout.flush()
            return 0
    return 1


def _acceptance_label(record: EffectRecord) -> tuple[str, str]:
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


def _acceptance_actions(state: AutoForgeState) -> list[dict]:
    """The derived ``acceptance_actions`` of ``status --json`` (#211).

    One entry per persisted K9 record of the current plan, then one per
    ``acceptance_outcomes`` entry (declined, stale or verified actions whose
    plan is gone), each with its ``state``: awaiting_approval, approved,
    authorized_by_policy, published_unverified, verified,
    verification_failed, declined or stale. ``verified`` comes only from an
    observed record or a recorded outcome, never from an acknowledgement.
    """
    from .claims import ACCEPTANCE, scan

    out: list[dict] = []
    for record in _acceptance_records(state):
        claim = scan(ACCEPTANCE, record.marker).claims[0]
        status, label = _acceptance_label(record)
        out.append(
            {
                "action_id": record.action_id,
                "kind": "issue_comment",
                "target": record.target["issue_url"],
                "pr": record.owner.pr_url,
                "round": claim.round,
                "finding_id": claim.finding_id,
                "authorization": record.precondition["authorization"],
                "state": status,
                "label": label,
                "payload_chars": len(record.body),
                "payload_sha256": sha256_text(record.body),
                "comment_url": str((record.observed or {}).get("url", "")),
            }
        )
    for outcome in state.acceptance_outcomes:
        out.append(
            {
                "action_id": outcome["action_id"],
                "kind": "issue_comment",
                "target": state.current_issue_url,
                "pr": state.current_pr_url,
                "round": outcome["round"],
                "finding_id": outcome["finding_id"],
                "state": outcome["outcome"],
                "label": (
                    f"verified: {outcome['comment_url']}"
                    if outcome["outcome"] == "verified"
                    else outcome["outcome"]
                ),
                "comment_url": outcome["comment_url"],
                "reason": outcome["reason"],
                "at": outcome["at"],
            }
        )
    return out


def _render_acceptance_actions(state: AutoForgeState, engine: ControllerEngine) -> list[str]:
    """The human ``Acceptance action`` blocks of ``status`` (#211); none without one."""
    from .claims import ACCEPTANCE, scan

    lines: list[str] = []
    records = state.phase_effects().records if state.mode != WorkflowMode.LOCAL else ()
    push = next((r for r in records if r.kind is EffectKind.PUSH), None)
    for record in _acceptance_records(state):
        claim = scan(ACCEPTANCE, record.marker).claims[0]
        action = record.action_id
        _, label = _acceptance_label(record)
        title = next(
            (
                str(f.get("title", ""))
                for f in state.open_findings
                if f.get("id") == claim.finding_id
            ),
            "",
        )
        if record.precondition["authorization"] == AUTHORIZATION_POLICY:
            authorization = "authorized by policy (safety.acceptance_actions: auto)"
        else:
            authorization = (
                "operator approval required (safety.acceptance_actions: "
                f"{engine.config.safety.acceptance_actions})"
            )
        lines += [
            f"Acceptance action {action} (FIX, review round {claim.round}, finding "
            f"{claim.finding_id})",
            "  kind:          issue_comment",
            f"  target:        {record.target['issue_url']}",
            f"  PR:            {record.owner.pr_url}",
            f"  finding:       {claim.finding_id} {title}".rstrip(),
            f"  authorization: {authorization}",
            f"  state:         {label}",
            f"  payload:       {len(record.body)} chars, sha256 "
            f"{sha256_text(record.body)}  ({engine.cli_command('status', '--action', action)})",
        ]
        if record.stage is Stage.OBSERVED:
            continue
        if push is not None and push.stage is not Stage.OBSERVED:
            branch = str(push.target["ref"]).removeprefix("refs/heads/")
            lines += [
                f"  held back:     push of {str(push.payload['sha'])[:12]} to {branch}, after "
                "the action is verified",
                f"  worktree:      {engine.agent_worktree_description()} holds the unpushed "
                "candidate; do not reset it",
            ]
        if record.stage is Stage.INTENDED and not record.authorized:
            why = '"..."'
            lines += [
                f"  next:          approve:  "
                f"{engine.cli_command('unblock', '--approve-action', action, '--reason')} {why}"
                f" ; {engine.resume_command()}",
                "                 or post exactly that payload yourself, then: "
                f"{engine.cli_command('unblock', '--reason')} {why} ; {engine.resume_command()}",
                "                 or decline: "
                f"{engine.cli_command('unblock', '--decline-action', action, '--reason')} {why}"
                " (FIX runs again)",
            ]
        elif record.stage is Stage.CONFLICT:
            lines.append(
                "  next:          repair or remove the comment(s) named above, then "
                f'{engine.cli_command("unblock", "--reason")} "..."'
            )
        else:
            lines.append(f"  next:          {engine.resume_command()}")
    for outcome in state.acceptance_outcomes if state.mode != WorkflowMode.LOCAL else ():
        label = (
            f"verified: {outcome['comment_url']}"
            if outcome["outcome"] == "verified"
            else outcome["outcome"]
        )
        reason = f" ({outcome['reason']})" if outcome["reason"] else ""
        lines.append(
            f"Acceptance action {outcome['action_id']} (review round {outcome['round']}, finding "
            f"{outcome['finding_id']}): {label}{reason}"
        )
    return lines


def _render_replan_journal(txn: ReplanTransaction | None) -> list[str]:
    """The human lines for a persisted replan journal; none without one.

    Shared by both modes so a journal a state carries is described the same
    way wherever it is found -- a LOCAL run never writes one, but a state
    file that has one anyway is shown, not silently skipped, and the human
    output then agrees with ``replan_journal`` in ``--json``.
    """
    if txn is None:
        return []
    lines = [
        f"Replan transaction: {txn.transaction_id or '(not yet created)'}",
        f"  stage:       {txn.stage.value}",
    ]
    if txn.journal_defects:
        lines.append(f"  journal:     CORRUPT ({'; '.join(txn.journal_defects)})")
    lines += [
        f"  source PR:   {txn.source_pr_url or '-'}",
        f"  replacement: {txn.replacement_pr_url or '-'}",
        f"  escalation:  {json.dumps(txn.escalation, sort_keys=True)}",
    ]
    if txn.rejection_reason:
        lines.append(f"  rejected:    {txn.rejection_reason}")
    return lines


def _render_status(
    state: AutoForgeState,
    txn: ReplanTransaction | None,
    acceptance: list[str] | None = None,
    evidence: list[str] | None = None,
) -> str:
    """Human status for a GitHub run (unredacted; the caller redacts)."""

    def _num(url: str) -> str:
        return url.rstrip("/").rsplit("/", 1)[-1] if url else "-"

    lines = [
        "AutoForge",
        "",
        f"Run:        {state.run_id}",
        f"Repository: {state.repository}",
        f"EPIC:       #{_num(state.epic_url)} {state.epic_url}",
        f"Issue:      #{_num(state.current_issue_url)} {state.current_issue_url or '-'}",
        f"PR:         #{_num(state.current_pr_url)} {state.current_pr_url or '-'}",
        f"Branch:     {state.current_branch or '-'}",
        f"Config:     {_recorded_configuration(state)}",
        "",
        f"Phase:      {state.phase.value}",
        f"Review rounds completed: {state.review_round}",
        f"Execution attempt: {state.execution_attempt}",
        f"Replan count: {state.escalation_count}",
        f"Current HEAD:  {state.current_head_sha or '-'}",
        f"Current base:  {state.current_base_ref or '-'}",
        f"Current merge base:  {state.current_merge_base_sha or '-'}",
        f"Reviewed PR:   {state.reviewed_pr_url or '-'}",
        f"Reviewed HEAD: {state.reviewed_head_sha or '-'}",
        f"Reviewed base: {state.reviewed_base_ref or '-'}",
        f"Reviewed merge base: {state.reviewed_merge_base_sha or '-'}",
        f"Last review:   {state.last_review_result or '-'}",
        f"Review comment: {state.last_review_comment_url or '-'}",
        f"Open findings: {len(state.open_findings)}",
        f"Prior findings to re-check: {len(state.prior_findings)}",
    ]
    if state.superseded_prs:
        lines.append("Superseded PRs:")
        for item in state.superseded_prs:
            lines.append(f"  {item.get('pr_url', '-')} -> {item.get('replacement_pr_url', '-')}")
    lines += _render_replan_journal(txn)
    if state.block_reason:
        lines.append(f"Reason:     {state.block_reason}")
    lines += acceptance or []
    if state.unblock_history:
        last = state.unblock_history[-1]
        action = f" [{last['action']}]" if last.get("action") else ""
        lines.append(
            f"Unblocked:  {len(state.unblock_history)} time(s); last at {last.get('at', '-')} "
            f"-> {last.get('phase', '-')}{action}: {last.get('reason', '-')}"
        )
    lines += evidence or []
    lines += [
        "",
        f"Steps executed: {state.step_count}",
        f"Merged since EPIC update: {state.merged_since_epic_update}",
        "",
        f"Created:    {state.created_at}",
        f"Updated:    {state.updated_at}",
    ]
    return "\n".join(lines)


def _recorded_configuration(state: AutoForgeState) -> str:
    """Where a REMOTE run's recorded configuration came from, and when (#219)."""
    if not state.config_fingerprint:
        return "not recorded (state from before #219; the next step, resume or unblock records it)"
    recorded = RecordedConfiguration.from_dict(state.config_fingerprint)
    return f"{recorded.origin()} ({recorded.source} {recorded.recorded_at})"


def _render_local_status(
    state: AutoForgeState, txn: ReplanTransaction | None, evidence: list[str] | None = None
) -> str:
    """Human status for a LOCAL run (unredacted; the caller redacts).

    Deliberately omits Issue/PR/branch/merge fields: a local run has none, and
    printing empty ones would suggest the GitHub lifecycle is merely stalled.
    A replan journal is shown only when the state carries one.
    """
    fix_budget = state.local_fix_rounds

    lines = [
        "AutoForge (local mode)",
        "",
        f"Run:        {state.run_id}",
        f"Mode:       {state.mode.value} (no GitHub: no issue, no PR, no push, no merge)",
        f"Feature:    {state.feature_spec_path or '-'}",
        f"Frozen SHA: {state.feature_spec_sha256 or '-'}",
        "",
        f"Phase:      {state.phase.value}",
        f"Review rounds completed: {state.review_round}",
        f"Fix rounds completed:    {fix_budget}",
        f"Last review:   {state.last_review_result or '-'}",
        f"Open findings: {len(state.open_findings)}",
    ]
    for finding in state.open_findings:
        lines.append(
            f"  {finding.get('id', '?')} [{finding.get('severity', '?')}] "
            f"{str(finding.get('title', '')).strip()[:80]}"
        )
    lines += [
        "",
        f"Base HEAD:   {state.base_head_sha or '(unborn)'}",
        f"Workspace fingerprint: {state.workspace_fingerprint or '-'}",
        f"Reviewed fingerprint:  {state.reviewed_workspace_fingerprint or '-'}",
    ]
    if state.baseline_dirty_paths:
        lines.append("Pre-existing working-tree changes at run creation (--allow-dirty):")
        for path in state.baseline_dirty_paths[:20]:
            lines.append(f"  {path}")
        if len(state.baseline_dirty_paths) > 20:
            lines.append(f"  ... and {len(state.baseline_dirty_paths) - 20} more")
    lines += _render_replan_journal(txn)
    if state.block_reason:
        lines.append(f"Reason:     {state.block_reason}")
    lines += evidence or []
    lines += [
        "",
        f"Steps executed: {state.step_count}",
        "",
        f"Created:    {state.created_at}",
        f"Updated:    {state.updated_at}",
    ]
    return "\n".join(lines)


# -- pretty printing ----------------------------------------------------------
def print_ready_banner(
    state: AutoForgeState,
    gate_open: bool = False,
    certificate: tuple[bool | None, str] | None = None,
) -> None:
    """The READY_FOR_MERGE banner; ``certificate`` is :func:`_review_certificate_check`'s answer.

    Whether the review certificate applies is read from state and the
    configuration in force, never GitHub: the merge gate compares it with
    the live PR itself (#175), and refuses to merge without one.
    """
    if not state.review_certificate:
        held = "none held; the merge gate would refuse to merge"
    elif certificate is None or certificate[0] is None:
        held = certificate[1] if certificate is not None else "not checked"
    elif certificate[0]:
        held = (
            f"applies (round {state.review_certificate.get('review_round')}, against state and "
            "the configuration in force; the merge gate checks it against GitHub too)"
        )
    else:
        held = f"does not apply ({certificate[1]}); the merge gate would refuse to merge"
    lines = [
        "",
        "=" * 72,
        "AutoForge workflow reached READY_FOR_MERGE.",
        f"  Issue:         {state.current_issue_url}",
        f"  PR:            {state.current_pr_url}",
        f"  Review round:  {state.review_round}",
        f"  Reviewed HEAD: {state.reviewed_head_sha}",
        f"  Reviewed base: {state.reviewed_base_ref or '-'}",
        f"  Reviewed merge base: {state.reviewed_merge_base_sha or '-'}",
        f"  Review:        {state.last_review_comment_url or '-'}",
        f"  Review certificate: {held}",
    ]
    if gate_open:
        lines += [
            "Merge gate is open but the step budget (--max-steps) ran out before MERGE.",
            "'resume --allow-merge' continues with the controller-side pre-merge verification.",
        ]
    else:
        lines += [
            "Automatic merge is disabled in this milestone.",
            "A human must review and merge the PR.",
        ]
    lines.append("=" * 72)
    # State-derived like the status output: the same redaction boundary.
    print(redact("\n".join(lines)))


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
        # The prompt is previewed below, so the argument that carries it is
        # shown as a placeholder. Only that argument: a provider that sends
        # the prompt on stdin has none, and its last argument is a flag value.
        shown = [
            "<prompt>" if plan.prompt_full and arg == plan.prompt_full else arg
            for arg in plan.command
        ]
        print(f"Command:  {' '.join(redact_argv(shown))}")
    if plan.limits is not None:
        print(f"Limits:   {plan.limits.describe()}")
    if plan.loop_detection is not None:
        print(f"Loops:    {plan.loop_detection.describe()}")
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
    # Every field here came out of state or an agent result, `run_id`
    # included, so each line is redacted whole rather than per field.
    print(redact(f"[{o.run_id}] {o.previous_phase} -> {o.next_phase}: {o.message}"))
    if o.plan is not None and o.plan.command:
        print(f"  profile={o.plan.profile_name} model={o.plan.model}")
    if o.result is not None:
        print(redact(f"  result={json.dumps(o.result, sort_keys=True)}"))


if __name__ == "__main__":
    raise SystemExit(main())
