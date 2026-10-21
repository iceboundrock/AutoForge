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
import io
import json
import os
import re
import select
import sys
import time
import unicodedata
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

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
    InteractionDataError,
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
from .interaction import (
    Allow,
    AllowScope,
    Answer,
    Cancel,
    DecisionKind,
    Deny,
    InteractionDecision,
    InteractionRequest,
    Question,
    QuestionResponse,
    RequestKind,
    Unavailable,
    UnavailableReason,
)
from .local_workspace import init_feature_file
from .locking import ControllerLock, repository_lock_path
from .ownership import refuse_scheduler_owned_issue, standalone_ownership_predicate
from .redaction import redact, redact_argv, redact_dict
from .replan_txn import ReplanTransaction
from .run_namespace import namespace_issue_of_state_dir
from .scheduler import EpicScheduler, SchedulerInputs
from .scheduler_stop import StopMode
from .sdk_policy import MAX_ANSWER_CHARS
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


# What one line typed at the terminal resolver may hold: an answer of
# MAX_ANSWER_CHARS characters of up to four UTF-8 bytes each, and a little
# more. A longer line is read to its end and refused, never kept.
_MAX_TERMINAL_LINE_BYTES = 4 * MAX_ANSWER_CHARS + 64
# An option number as typed: ASCII digits, and few of them (a question has
# at most sdk_policy.MAX_OPTIONS options), so int() takes every one.
_OPTION_NUMBER = re.compile(r"[0-9]{1,3}")
# What the resolver may have written to a terminal that has not taken it yet
# (its output suspended, or not read): past this it reads no more keys until
# the terminal takes some or the deadline comes, so what waits stays bounded.
_MAX_UNSHOWN_BYTES = 64 * 1024


class _Unusable(Exception):
    """A line the terminal resolver cannot use: the message names the rule."""


class _TerminalWriter:
    """Writes to the terminal of ``fd`` that never block (#134).

    A terminal's output can be suspended (its stop key under IXON, another
    program's ``tcflow``) or left unread, and a blocking write then waits
    until it resumes: past any deadline, and the resolver with it. So the
    resolver writes to a terminal through a descriptor of its own on its
    device, opened non-blocking: an open file description of its own, so
    stdin, stderr and the shell's descriptors stay as they are. What the
    terminal does not take at once waits here, and is written as it takes
    it while the resolver waits for a key, never past the deadline
    (:meth:`_Terminal.line`); what it has not taken when the resolver is
    done is dropped (:meth:`close`).
    """

    def __init__(self, fd: int) -> None:
        self.fd = os.open(os.ttyname(fd), os.O_WRONLY | os.O_NOCTTY | os.O_CLOEXEC | os.O_NONBLOCK)
        self.device = os.fstat(self.fd).st_rdev
        self._unshown = bytearray()

    @property
    def unshown(self) -> int:
        """How many bytes the terminal has not taken yet."""
        return len(self._unshown)

    def write(self, data: bytes) -> None:
        self._unshown += data
        self.flush()

    def flush(self) -> None:
        """Write what the terminal takes now."""
        while self._unshown:
            try:
                written = os.write(self.fd, self._unshown)
            except BlockingIOError:
                return
            if not written:
                return
            del self._unshown[:written]

    def close(self) -> None:
        try:
            self.flush()
        except OSError:  # the terminal hung up: there is no one to show it to
            pass
        finally:
            os.close(self.fd)


class _LineEditing:
    """A terminal whose line editing the resolver took over: its keys, the
    writer it echoes what is typed through, and the settings it reads under
    (:func:`_line_editing`)."""

    def __init__(self, fd: int, saved: list, writer: _TerminalWriter) -> None:
        import termios

        cc = saved[6]

        def key(index: int) -> int | None:
            value = cc[index]
            code = value[0] if isinstance(value, bytes) else value
            # 0 is Linux's _POSIX_VDISABLE, 0xff macOS's; a key past ASCII
            # would cut into a UTF-8 character.
            return code if 0 < code < 0x80 else None

        # Backspace and delete erase whatever the terminal's erase key is:
        # a control character is never part of a usable answer.
        self.erase = frozenset(k for k in (key(termios.VERASE), 0x08, 0x7F) if k is not None)
        self.word_erase = key(termios.VWERASE)
        self.kill = key(termios.VKILL)
        self.eof = key(termios.VEOF)
        # Under IXON the stop and start keys are never input either.
        flow = (key(termios.VSTOP), key(termios.VSTART)) if saved[0] & termios.IXON else ()
        self.flow = frozenset(k for k in flow if k is not None)
        self._fd = fd
        self.writer = writer
        # Canonical mode, echo and output flow control off, and a read
        # returns once a key is typed.
        self.raw = list(saved)
        self.raw[0] = saved[0] & ~termios.IXON
        self.raw[3] = saved[3] & ~(termios.ICANON | termios.ECHO)
        self.raw[6] = list(cc)
        self.raw[6][termios.VMIN], self.raw[6][termios.VTIME] = 1, 0

    def echo(self, data: bytes) -> None:
        self.writer.write(data)

    def retake(self) -> bool:
        """Whether the terminal's own line editing came back on while the
        resolver waited (a stop and continue under a shell that resets the
        terminal, another program's ``stty``): what was just read went
        through it and may have been cut. It is turned off again, and so is
        output flow control when that came back on."""
        import termios

        attrs = termios.tcgetattr(self._fd)
        if attrs[3] & (termios.ICANON | termios.ECHO) or attrs[0] & termios.IXON:
            termios.tcsetattr(self._fd, termios.TCSANOW, self.raw)
        return bool(attrs[3] & termios.ICANON)


@contextmanager
def _line_editing(fd: int) -> Iterator[_LineEditing | None]:
    """Take over the line editing of the terminal ``fd`` while the resolver
    asks (#134); ``None`` for a descriptor that is not a terminal, whose
    lines arrive whole.

    In canonical mode a terminal edits a line before a reader gets it, and
    Linux keeps at most 4095 bytes of it: what is typed past that is dropped
    without a word, under any bound a reader could hold the line to, and an
    answer or guidance would reach the agent cut short. So canonical mode
    and echo are off while the resolver reads, and it edits the line itself
    (:class:`_Terminal`): it echoes what it keeps, on the terminal and
    never on stderr, and takes the terminal's own erase, word-erase, kill
    and end-of-file keys. A line read while canonical mode came back on is
    refused (:meth:`_LineEditing.retake`).

    Output flow control (IXON) is off too: under it the stop key suspends
    the terminal's output until the start key, and the echo would wait for
    it past the deadline. The two keys are still never input, and suspend
    nothing; output a stop key suspended before the resolver asked comes
    back where turning IXON off restarts it (Linux). Whatever else holds the
    output, the resolver writes to the terminal only as it takes it
    (:class:`_TerminalWriter`). Everything else stays the terminal's: Ctrl-C
    still raises ``KeyboardInterrupt`` (ISIG is untouched), and the settings
    are restored however the reading ends.
    """
    try:
        import termios
    except ImportError:  # pragma: no cover - a platform without termios
        yield None
        return
    try:
        saved = termios.tcgetattr(fd)
    except (OSError, termios.error):
        saved = None
    if saved is None:
        yield None
        return
    # Its own descriptor, opened to write, also echoes a terminal read
    # through a read-only one.
    writer = _TerminalWriter(fd)
    try:
        editing = _LineEditing(fd, saved, writer)
        termios.tcsetattr(fd, termios.TCSANOW, editing.raw)
        try:
            yield editing
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
    finally:
        writer.close()


def _in_memory(output: TextIO | None) -> bool:
    """Whether ``output`` is a stream without a descriptor (a StringIO),
    whose writes never wait. ``None`` is no stream at all: Python's stderr
    when its descriptor was closed at startup; a closed stream is none
    either."""
    if output is None:
        return False
    try:
        output.fileno()
    except (AttributeError, io.UnsupportedOperation):
        return True
    except (OSError, ValueError):
        return False
    return False


def _terminal_fd(output: TextIO | None) -> int | None:
    """The descriptor ``output`` writes to when that is a terminal."""
    if output is None:
        return None
    try:
        fd = output.fileno()
    except (AttributeError, OSError, ValueError):
        return None
    return fd if os.isatty(fd) else None


def _can_show(output: TextIO | None, editing: _LineEditing | None) -> bool:
    """Whether the resolver has somewhere to show its text that no write
    holds past the deadline (:func:`_shown_on`)."""
    return editing is not None or _in_memory(output) or _terminal_fd(output) is not None


@contextmanager
def _shown_on(
    output: TextIO | None, editing: _LineEditing | None
) -> Iterator[_TerminalWriter | None]:
    """The writer the resolver shows its text through, so that no write holds
    it past the deadline (R4-F1).

    An ``output`` that is a terminal is written to as it takes it: through
    the echo's writer when it is the terminal the resolver reads, so that
    the two keep their order. Any other output (stderr redirected to a
    pipe, a file or a socket: ``2>log``, ``2>&1 | tee log``; or closed) is
    never written to, since a pipe no one reads holds a blocking write
    until it is read: the text is shown on the terminal the resolver reads
    instead, where the answer is typed, as ``getpass`` does. ``None`` for
    an in-memory stream, printed to as before. Entered only when
    :func:`_can_show`: with no terminal to show the text on instead, the
    resolver asks nothing."""
    if _in_memory(output):
        yield None
        return
    fd = _terminal_fd(output)
    if editing is not None and (fd is None or os.fstat(fd).st_rdev == editing.writer.device):
        yield editing.writer
        return
    assert fd is not None  # _can_show
    writer = _TerminalWriter(fd)
    try:
        yield writer
    finally:
        writer.close()


def _utf8_length(lead: int) -> int:
    """How many bytes the UTF-8 character starting with ``lead`` has; 1 for
    a byte no character starts with (kept alone, so its line is refused)."""
    if 0xC2 <= lead <= 0xDF:
        return 2
    if 0xE0 <= lead <= 0xEF:
        return 3
    if 0xF0 <= lead <= 0xF4:
        return 4
    return 1


def _echoed(typed: bytes) -> tuple[bytes, int]:
    """What the terminal is shown of one typed character, and its columns: a
    control character as ^X, one that is not UTF-8 or would act on the
    terminal as U+FFFD, a combining mark in no column of its own and a wide
    character in two."""
    try:
        ch = typed.decode("utf-8")
    except UnicodeDecodeError:
        return "\ufffd".encode(), 1
    if ord(ch) < 0x20:
        return b"^" + bytes((ord(ch) + 0x40,)), 2
    category = unicodedata.category(ch)
    if category in ("Cc", "Cf", "Zl", "Zp"):
        return "\ufffd".encode(), 1
    if category in ("Mn", "Me"):
        return typed, 0
    return typed, 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class _Terminal:
    """The operator's terminal while the resolver asks: lines read from
    ``fd`` until ``deadline`` (a string, or :class:`Unavailable`), and the
    resolver's text shown on ``output`` (:meth:`show`).

    At a terminal (``editing``) each key is taken as it is typed and the
    line is edited here (:func:`_line_editing`); any other descriptor
    delivers whole lines. A line past :data:`_MAX_TERMINAL_LINE_BYTES` is
    refused whole when it ends: at a terminal what is typed past the bound
    is neither kept nor echoed (the bell rings once), and the kill key
    starts the line again.

    What the resolver writes to a terminal, its echo and, unless ``output``
    is an in-memory stream (``shown``, :func:`_shown_on`), its text, goes
    through a :class:`_TerminalWriter`: what the terminal has not taken is
    written while a line is awaited, and a terminal whose output is
    suspended never holds the resolver past the deadline. While more than
    :data:`_MAX_UNSHOWN_BYTES` wait, no key is read.
    """

    def __init__(
        self,
        fd: int,
        clock: Callable[[], float],
        deadline: float,
        editing: _LineEditing | None = None,
        output: TextIO | None = None,
        shown: _TerminalWriter | None = None,
    ) -> None:
        self._fd = fd
        self._clock = clock
        self._deadline = deadline
        self._editing = editing
        self._output = output
        self._shown = shown
        self._writers: list[_TerminalWriter] = []
        for writer in (editing.writer if editing is not None else None, shown):
            if writer is not None and writer not in self._writers:
                self._writers.append(writer)
        self._pending = b""
        self._overlong = False
        # The line being typed at a terminal: each character's bytes and
        # columns, and the first bytes of one not yet whole.
        self._typed: list[tuple[bytes, int]] = []
        self._size = 0
        self._partial = b""

    def show(self, text: str, end: str = "\n") -> None:
        if self._shown is None:  # an in-memory stream (_shown_on)
            print(text, end=end, file=self._output, flush=True)
            return
        encoding = getattr(self._output, "encoding", None) or "utf-8"
        self._shown.write((text + end).encode(encoding, "backslashreplace"))

    def line(self) -> str | Unavailable:
        while (raw := self._take()) is None:
            remaining = self._deadline - self._clock()
            if remaining <= 0:
                return Unavailable(UnavailableReason.EXPIRED)
            unshown = [writer for writer in self._writers if writer.unshown]
            reading = [self._fd] if sum(w.unshown for w in unshown) < _MAX_UNSHOWN_BYTES else []
            readable, writable, _ = select.select(reading, [w.fd for w in unshown], [], remaining)
            for writer in unshown:
                if writer.fd in writable:
                    writer.flush()
            if not readable:
                if writable:
                    continue
                return Unavailable(UnavailableReason.EXPIRED)
            chunk = os.read(self._fd, 4096)
            if not chunk:
                return Unavailable(UnavailableReason.END_OF_INPUT)
            if self._editing is not None and self._editing.retake():
                self._pending, self._partial, self._overlong = b"", b"", False
                self._typed, self._size = [], 0
                raise _Unusable("the terminal's line editing came back on while the line was typed")
            self._pending += chunk
        if isinstance(raw, Unavailable):
            return raw
        overlong, self._overlong = self._overlong, False
        if overlong:
            raise _Unusable("the line is too long")
        try:
            return raw.decode("utf-8").rstrip("\r")
        except UnicodeDecodeError:
            raise _Unusable("the line is not UTF-8") from None

    def seconds_left(self) -> int:
        return max(0, int(self._deadline - self._clock()))

    def _take(self) -> bytes | Unavailable | None:
        """The next line of what was read, or ``None`` until one has ended."""
        if self._editing is not None:
            return self._edit(self._editing)
        raw, newline, rest = self._pending.partition(b"\n")
        if not newline:
            if len(self._pending) > _MAX_TERMINAL_LINE_BYTES:
                self._pending, self._overlong = b"", True
            return None
        self._pending = rest
        if len(raw) > _MAX_TERMINAL_LINE_BYTES:
            self._overlong = True
        return raw

    def _edit(self, keys: _LineEditing) -> bytes | Unavailable | None:
        pending, self._pending = self._pending, b""
        for at, byte in enumerate(pending):
            if byte in keys.flow:  # the stop and start keys, taken as under IXON
                continue
            if self._partial and 0x80 <= byte <= 0xBF:
                self._partial += bytes((byte,))
                if len(self._partial) == _utf8_length(self._partial[0]):
                    self._keep(self._partial, keys)
                    self._partial = b""
                continue
            if self._partial:  # a character cut short: kept, so the line is refused
                self._keep(self._partial, keys)
                self._partial = b""
            if byte >= 0x80:
                if _utf8_length(byte) == 1:
                    self._keep(bytes((byte,)), keys)
                else:
                    self._partial = bytes((byte,))
            elif byte in b"\r\n":
                keys.echo(b"\n")
                self._pending = pending[at + 1 :]
                line = b"".join(typed for typed, _ in self._typed)
                self._typed, self._size = [], 0
                return line
            elif byte in keys.erase:
                self._erase(1, keys)
            elif byte == keys.word_erase:
                while self._typed and self._typed[-1][0].decode("utf-8", "replace").isspace():
                    self._erase(1, keys)
                while self._typed and not self._typed[-1][0].decode("utf-8", "replace").isspace():
                    self._erase(1, keys)
            elif byte == keys.kill:
                self._erase(len(self._typed), keys)
                self._overlong = False
            elif byte == keys.eof:
                # As in canonical mode: the end of input on an empty line,
                # and nothing within one.
                if not self._typed and not self._overlong:
                    return Unavailable(UnavailableReason.END_OF_INPUT)
            else:
                self._keep(bytes((byte,)), keys)
        return None

    def _keep(self, typed: bytes, keys: _LineEditing) -> None:
        if self._overlong or self._size + len(typed) > _MAX_TERMINAL_LINE_BYTES:
            if not self._overlong:
                keys.echo(b"\a")
            self._overlong = True
            return
        shown, columns = _echoed(typed)
        keys.echo(shown)
        self._typed.append((typed, columns))
        self._size += len(typed)

    def _erase(self, count: int, keys: _LineEditing) -> None:
        for _ in range(min(count, len(self._typed))):
            typed, columns = self._typed.pop()
            self._size -= len(typed)
            keys.echo(b"\b" * columns + b" " * columns + b"\b" * columns)


def _discard_typeahead(fd: int) -> None:
    """Drop what was typed before the request was shown, so a key pressed
    earlier never answers it. Nothing to drop on a descriptor that is not a
    terminal."""
    try:
        import termios
    except ImportError:  # pragma: no cover - a platform without termios
        return
    try:
        termios.tcflush(fd, termios.TCIFLUSH)
    except (OSError, termios.error):
        pass


class TerminalResolver:
    """Asks the operator at this terminal (ADR 0007 D5.1, D7.4, D7.7; #134).

    The engine's resolver for a real ``run``, ``local run``, ``step`` and
    ``resume``. It reads stdin and shows the request on stderr, where the
    live progress is, when stderr is a terminal; stdout stays the step
    outcomes'. A redirected stderr (a pipe, a file) or none at all is never
    written to: the request is shown on the terminal it reads instead
    (:func:`_shown_on`). Before it asks it checks that stdin is a terminal
    (D7.4) and drops anything typed ahead. While it asks it edits the line
    itself, since the terminal's own line editing would drop what is typed
    past its buffer without a word (:func:`_line_editing`): an answer
    reaches the agent whole or is refused. Each line is read until the
    deadline the broker gives: its expiry is ``expired``, the end of the
    input ``end_of_input``, and the broker decides what either means.
    No write holds it past that deadline: the stop key suspends nothing
    while it reads, it writes to a terminal only as the terminal takes it
    (:class:`_TerminalWriter`), so a terminal whose output is suspended or
    unread neither holds a decision back nor keeps the wait open, and it
    writes to no pipe, which a reader that stopped reading would hold.
    Ctrl-C raises ``KeyboardInterrupt`` as anywhere else: the step is
    interrupted and nothing is decided (D7.7). An input it cannot use is
    explained, by rule and never by value, and asked again within the same
    deadline. Only what the request offers is offered; a modified input
    never is, here.

    Everything shown is the request's own rendering, already bounded,
    cleaned and redacted (:mod:`autoforge.interaction`).
    """

    def __init__(
        self,
        *,
        input_fd: int | None = None,
        output: TextIO | None = None,
        clock: Callable[[], float] = time.monotonic,
        is_terminal: Callable[[int], bool] = os.isatty,
    ) -> None:
        self._input_fd = input_fd
        self._output = output
        self._clock = clock
        self._is_terminal = is_terminal

    def resolve(
        self, request: InteractionRequest, deadline: float
    ) -> InteractionDecision | Unavailable:
        fd = self._stdin()
        if fd is None or not self._is_terminal(fd):
            return Unavailable(UnavailableReason.NO_TERMINAL)
        _discard_typeahead(fd)
        output = self._output or sys.stderr
        with _line_editing(fd) as editing:
            if not _can_show(output, editing):
                return Unavailable(UnavailableReason.NO_TERMINAL)
            with _shown_on(output, editing) as shown:
                terminal = _Terminal(fd, self._clock, deadline, editing, output, shown)
                return self._resolve(request, terminal)

    # -- internals -------------------------------------------------------------
    def _resolve(
        self, request: InteractionRequest, terminal: _Terminal
    ) -> InteractionDecision | Unavailable:
        source = f", subagent {request.agent}" if request.agent is not None else ""
        if request.kind is RequestKind.PERMISSION:
            terminal.show(
                f"autoforge: the agent asks permission to use {request.tool_name} "
                f"(request {request.request_id}{source}); answer within "
                f"{terminal.seconds_left()}s, Ctrl-C stops the step"
            )
            return self._permission(request, terminal)
        terminal.show(
            f"autoforge: the agent asks {len(request.questions)} question(s) "
            f"(request {request.request_id}{source}); answer within "
            f"{terminal.seconds_left()}s, Ctrl-C stops the step"
        )
        responses: list[QuestionResponse] = []
        for n, question in enumerate(request.questions, 1):
            response = self._question(request, question, n, terminal)
            if not isinstance(response, QuestionResponse):
                return response
            responses.append(response)
        return Answer(tuple(responses))

    def _stdin(self) -> int | None:
        if self._input_fd is not None:
            return self._input_fd
        try:
            return sys.stdin.fileno() if sys.stdin is not None else None
        except (OSError, ValueError):
            return None

    def _ask(self, terminal: _Terminal, prompt: str) -> str | Unavailable:
        """One usable line for ``prompt``, or :class:`Unavailable`."""
        while True:
            terminal.show(prompt, end="")
            try:
                line = terminal.line()
            except _Unusable as exc:
                terminal.show(f"  not used: {exc}; try again")
                continue
            if isinstance(line, Unavailable):
                terminal.show("")
                return line
            if line.strip():
                return line.strip()

    def _question(
        self,
        request: InteractionRequest,
        question: Question,
        n: int,
        terminal: _Terminal,
    ) -> QuestionResponse | Cancel | Unavailable:
        own = question.custom and DecisionKind.CUSTOM_ANSWER in request.offers
        cancel = DecisionKind.CANCEL in request.offers
        title = f"  ({n}/{len(request.questions)})"
        terminal.show(f"{title} {question.header}" if question.header else title)
        terminal.show(f"  {question.text}")
        for number, option in enumerate(question.options, 1):
            described = f" - {option.description}" if option.description else ""
            terminal.show(f"    {number}. {option.shown}{described}")
        if own:
            terminal.show("    0. something else, in your own words")
        several = "numbers separated by commas" if question.multiple else "a number"
        prompt = f"  choose {several}{', or c to cancel' if cancel else ''}: "
        while True:
            line = self._ask(terminal, prompt)
            if isinstance(line, Unavailable):
                return line
            if line.lower() == "c" and cancel:
                return Cancel()
            picked = self._picks(line, question, own)
            if isinstance(picked, str):
                terminal.show(f"  not used: {picked}; try again")
                continue
            custom: str | None = None
            if 0 in picked:
                text = self._ask(terminal, "  your answer: ")
                if isinstance(text, Unavailable):
                    return text
                custom = text
            labels = tuple(question.options[number - 1].label for number in picked if number)
            try:
                return QuestionResponse(choices=labels, custom_text=custom)
            except InteractionDataError as exc:
                terminal.show(f"  not used: {exc}; try again")

    @staticmethod
    def _picks(line: str, question: Question, own: bool) -> list[int] | str:
        """The option numbers ``line`` picks, or why it picks none."""
        words = [word.strip() for word in line.split(",")]
        if not all(_OPTION_NUMBER.fullmatch(word) for word in words):
            return "expected option numbers"
        picked = [int(word) for word in words]
        lowest = 0 if own else 1
        if any(not lowest <= number <= len(question.options) for number in picked):
            return f"choose from {lowest} to {len(question.options)}"
        if len(set(picked)) != len(picked):
            return "an option is chosen twice"
        if len(picked) > 1 and not question.multiple:
            return "this question takes one answer"
        return picked

    def _permission(
        self, request: InteractionRequest, terminal: _Terminal
    ) -> Allow | Deny | Cancel | Unavailable:
        offers = request.offers
        if request.summary:
            terminal.show(f"  {request.summary}")
        terminal.show(f"  input sha256 {request.input_sha256}")
        choices: dict[str, str] = {}
        if DecisionKind.ALLOW_ONCE in offers:
            choices["y"] = "allow this call once"
        if DecisionKind.ALLOW_SESSION in offers:
            choices["s"] = "allow it for the rest of this session"
        if DecisionKind.DENY in offers or DecisionKind.DENY_GUIDANCE in offers:
            choices["n"] = (
                "deny it (n <text>: and tell the agent what to do instead)"
                if DecisionKind.DENY_GUIDANCE in offers
                else "deny it"
            )
        if DecisionKind.CANCEL in offers:
            choices["c"] = "cancel: decline to decide"
        for key, text in choices.items():
            terminal.show(f"    {key}  {text}")
        while True:
            line = self._ask(terminal, f"  choose {'/'.join(choices)}: ")
            if isinstance(line, Unavailable):
                return line
            key, _, rest = line.partition(" ")
            key, rest = key.lower(), rest.strip()
            if key not in choices or (rest and key != "n"):
                terminal.show(f"  not used: expected one of {', '.join(choices)}; try again")
                continue
            if key == "y":
                return Allow(AllowScope.ONCE)
            if key == "s":
                return Allow(AllowScope.SESSION)
            if key == "c":
                return Cancel()
            if not rest and DecisionKind.DENY not in offers:
                terminal.show("  not used: say what the agent should do instead (n <text>)")
                continue
            if rest and DecisionKind.DENY_GUIDANCE not in offers:
                terminal.show("  not used: this provider takes no guidance with a denial (n)")
                continue
            try:
                return Deny(rest or None)
            except InteractionDataError as exc:
                terminal.show(f"  not used: {exc}; try again")


def _attach_terminal(engine: ControllerEngine) -> None:
    """Stream a real run's agent progress to stderr (#192), and ask the
    operator at this terminal when an agent asks for a decision (ADR 0007
    D5.1, #134; only a profile whose ``interaction_policy`` is ``prompt``
    ever reaches it).

    Only the non-dry paths call this: a dry run launches nothing. stdout is
    left to the step outcomes, so nothing about progress or a question
    reaches output a script reads.
    """
    engine.progress_output = _progress_line
    engine.interaction_resolver = TerminalResolver()


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
        _attach_terminal(engine)
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
        _attach_terminal(engine)
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
        _attach_terminal(engine)
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
        _attach_terminal(engine)
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
