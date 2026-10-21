"""The live Pi smoke test (#135): its flow, its checks and its report.

``tests/test_live_pi.py`` runs :func:`run_smoke` against the real ``pi`` and
a ChatGPT sign-in the operator made beforehand; ``tests/test_live_pi_support.py``
runs the same flow against the fake ``pi`` of ``tests/pi_fake.py``, offline,
and tests each check on its own. One run is seven steps
(docs/development.md, "The live Pi smoke test"):

1. preconditions, checked and never performed: ``pi`` is installed, at
   least :data:`~autoforge.providers.PI_MIN_VERSION`, and signed in;
2. ``OPENAI_API_KEY`` is absent from this process's environment;
3. ``autoforge local doctor`` (the real CLI, in a process of its own) passes
   its Pi rows: Pi available, auth ready with ``authType oauth``;
4. one LOCAL REVIEW on Pi returns a valid ``CONTROL_RESULT``: a scripted
   stand-in implements a one-line feature, so the review is the only model
   call (``execution.max_correction_attempts: 0``);
5. nothing AutoForge wrote holds a credential-shaped string;
6. the review left no side effect AutoForge promised against: the tree is
   unchanged, Pi's session directory gained no entry, no process is left;
7. the report: what ran and each step's outcome.

What this never does: start ``/login`` or Pi interactively, read a file in
Pi's agent directory (``auth.json`` included; the session check lists names
only), pass ``--credentials`` or a ``print-*`` subcommand, or print a token,
an account id, an email or a plan name. A model's or Pi's own error text is
not printed either, since it may name the plan: a failure is reported by its
category (#131's failure reasons), never by its detail.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from autoforge.config import ProfileConfig, load_config_file
from autoforge.engine import ControllerEngine
from autoforge.providers import (
    PI_DEFAULT_AGENT_DIR,
    PI_MIN_VERSION,
    AgentExecutionResult,
    AgentRequest,
    PiProvider,
    ProviderRegistry,
    ScriptedProvider,
    parse_pi_version,
)
from autoforge.redaction import redact
from autoforge.transitions import Phase

# `AUTOFORGE_LIVE_PI=1` (and `-m live`) runs the smoke test; any other
# non-empty value is refused rather than read as "no".
LIVE_VARIABLE = "AUTOFORGE_LIVE_PI"
# The model and thinking level of the review: an account may not have the
# default model, and a non-reasoning model needs `off`.
MODEL_VARIABLE = "AUTOFORGE_LIVE_PI_MODEL"
EFFORT_VARIABLE = "AUTOFORGE_LIVE_PI_EFFORT"
DEFAULT_MODEL = "openai/gpt-5.6-terra"
DEFAULT_EFFORT = "low"
# The review's limits: it reads two small files.
IDLE_TIMEOUT_SECONDS = 300
MAX_RUNTIME_SECONDS = 900
# Pi's own read-only commands (`--version`, `auth check`) and doctor.
PROBE_TIMEOUT_SECONDS = 120

STEPS = (
    "1. preconditions: pi installed, recent enough, signed in",
    "2. no OPENAI_API_KEY in AutoForge's environment",
    "3. doctor: pi available, pi auth ready with authType oauth",
    "4. one REVIEW on Pi returns a valid CONTROL_RESULT",
    "5. nothing AutoForge wrote holds a credential-shaped string",
    "6. no Pi side effect: tree unchanged, no session file, no process left",
)

FEATURE = "features/greeting.md"
FEATURE_MD = """# Feature: Greeting helper

## Problem

The project cannot greet someone by name.

## Requirements

- `greet(name)` in `greet.py` returns `Hello, <name>!`.

## Acceptance Criteria

- [ ] `greet("Ada")` returns `"Hello, Ada!"`.

## Non-goals

- Anything else.

## Notes / Decisions

- A throwaway repository of AutoForge's live Pi smoke test.
"""
IMPLEMENTATION = "greet.py"
IMPLEMENTATION_PY = '''def greet(name: str) -> str:
    """Return a greeting for ``name``."""
    return f"Hello, {name}!"
'''


# -- credential-shaped strings (step 5) ---------------------------------------------
# Each is searched for in every file AutoForge wrote. A redacted value still
# counts where its shape survives redaction (`"refresh": "***REDACTED***"`,
# `Bearer ***REDACTED***`): the run must not have produced credential
# material at all, redaction is only the second line. The JSON field names
# also match when escaped inside a JSON string (`\"refresh\"`).
CREDENTIAL_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("jwt", re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]*")),
    ("bearer", re.compile(r"\bBearer\s+\S")),
    ("refresh_token", re.compile(r"(?i)refresh_token")),
    ('"refresh"', re.compile(r'"refresh\\*"')),
    ('"access"', re.compile(r'"access\\*"')),
    ("chatgpt-account-id", re.compile(r"(?i)chatgpt-account-id")),
    ("chatgpt_account_id", re.compile(r"(?i)chatgpt_account_id")),
    ("sk- key", re.compile(r"(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{16,}")),
)


def credential_hits(text: str) -> list[str]:
    """The names of the patterns ``text`` matches, in pattern order; never a match."""
    return [name for name, pattern in CREDENTIAL_PATTERNS if pattern.search(text)]


def scan_for_credentials(root: Path) -> list[tuple[str, str]]:
    """``(path relative to root, pattern name)`` for each credential-shaped
    string in a file under ``root``. Symbolic links are not followed; the
    matched text is never returned."""
    hits: list[tuple[str, str]] = []
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", errors="replace")
            relative = str(path.relative_to(root))
            hits.extend((relative, hit) for hit in credential_hits(text))
    return hits


# -- side effects (step 6) ----------------------------------------------------------
def pi_agent_dir(env: Mapping[str, str] = os.environ) -> Path:
    """The agent directory a Pi launch uses: ``PI_CODING_AGENT_DIR``, which the
    launch allow-list passes, or Pi's default."""
    return Path(env.get("PI_CODING_AGENT_DIR") or PI_DEFAULT_AGENT_DIR).expanduser()


def pi_session_dir(env: Mapping[str, str] = os.environ) -> Path:
    """Where Pi writes a session without ``--no-session``: ``sessions`` in its
    agent directory (ADR 0003 §2.4). ``PI_CODING_AGENT_SESSION_DIR`` never
    reaches a launch; a ``sessionDir`` in Pi's settings is not looked at,
    since that would read a file in the agent directory."""
    return pi_agent_dir(env) / "sessions"


def entry_names(directory: Path) -> frozenset[str]:
    """Every entry under ``directory`` by relative path, or none when it does
    not exist. Names only: no file is opened, no link followed."""
    names: set[str] = set()
    for parent, dirs, files in os.walk(directory):
        for name in [*dirs, *files]:
            names.add(str((Path(parent) / name).relative_to(directory)))
    return frozenset(names)


def tree_snapshot(root: Path) -> dict[str, str]:
    """Every entry of a working tree with a digest of what it holds, outside
    its ``.git`` and the ``__pycache__`` directories the smoke config excludes
    (``local.exclude``): an independent second look at what the LOCAL
    fingerprint binds."""
    snapshot: dict[str, str] = {}
    for parent, dirs, files in os.walk(root):
        dirs[:] = [
            d for d in dirs if d != "__pycache__" and not (Path(parent) == root and d == ".git")
        ]
        for name in dirs:
            snapshot[str((Path(parent) / name).relative_to(root))] = "directory"
        for name in files:
            path = Path(parent) / name
            if path.is_symlink():
                held = "link " + os.readlink(path)
            else:
                held = f"{path.stat().st_mode:o} " + hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot[str(path.relative_to(root))] = held
    return snapshot


def processes_under(path: Path) -> list[int]:
    """The processes other than this one whose command line names ``path``
    or whose working directory lies in it; on a platform without ``/proc``,
    by command line only (``ps``)."""
    token = os.path.realpath(path)
    own = os.getpid()
    found: list[int] = []
    if os.path.isdir("/proc/self"):
        for entry in os.scandir("/proc"):
            if not entry.name.isdigit() or int(entry.name) == own:
                continue
            try:
                with open(f"/proc/{entry.name}/cmdline", "rb") as fh:
                    command = fh.read().replace(b"\0", b" ").decode("utf-8", "replace")
            except OSError:
                continue  # gone already
            try:
                cwd = os.readlink(f"/proc/{entry.name}/cwd")
            except OSError:
                cwd = ""  # another user's process, or gone
            if token in command or cwd == token or cwd.startswith(token + os.sep):
                found.append(int(entry.name))
        return sorted(found)
    listing = subprocess.run(
        ["ps", "-A", "-ww", "-o", "pid=", "-o", "args="],
        capture_output=True,
        text=True,
        check=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    ).stdout
    for line in listing.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != own and token in command:
            found.append(int(pid))
    return sorted(found)


# -- failure reasons (step 4) --------------------------------------------------------
# #131's reasons for a run the provider side refused: the sign-in, the
# account's limits or model access, the model itself. Every other reason
# (a protocol violation, a model or thinking mismatch, an early exit, a
# dialog) is AutoForge's or the contract's.
PROVIDER_REFUSALS = (
    "the auth preflight refused the launch",
    "prompt rejected",
    "model error",
    "subscription usage limit reached",
    "model unavailable",
    "the model hit its output limit",
    "the run was aborted",
)


def failure_category(provider_failure: str) -> str:
    """The category of a Pi ``provider_failure``, without its detail:
    ``pi: model error after acceptance: <text>`` is ``model error after
    acceptance``."""
    return provider_failure.removeprefix("pi: ").split(":", 1)[0].strip()


def classify_failure(provider_failure: str) -> str:
    """``provider refused (<category>)`` or ``AutoForge failure (<category>)``."""
    category = failure_category(provider_failure)
    if category.startswith(PROVIDER_REFUSALS):
        return f"provider refused ({category})"
    return f"AutoForge failure ({category})"


# -- the report (step 7) -------------------------------------------------------------
class StepFailed(Exception):
    """A step's own finding: its message is the controller's text, never Pi's."""


def require(condition: object, message: str) -> None:
    if not condition:
        raise StepFailed(message)


def _first_line(text: str, limit: int = 300) -> str:
    line = redact(text).strip().splitlines()[0] if text.strip() else ""
    return line if len(line) <= limit else line[: limit - 3] + "..."


@dataclass
class SmokeReport:
    """Each step's outcome and the facts of the run, rendered without a credential."""

    outcomes: dict[str, tuple[bool, str]] = field(default_factory=dict)
    facts: dict[str, str] = field(default_factory=dict)

    @contextmanager
    def step(self, name: str) -> Iterator[None]:
        """Run one step: it passes, or fails with its message, and the next goes on."""
        try:
            yield
        except StepFailed as exc:
            self.outcomes[name] = (False, str(exc))
        except Exception as exc:
            self.outcomes[name] = (False, f"{type(exc).__name__}: {_first_line(str(exc))}")
        else:
            self.outcomes[name] = (True, "")

    def passed(self, name: str) -> bool:
        return self.outcomes.get(name, (False, ""))[0]

    @property
    def ok(self) -> bool:
        return all(self.passed(name) for name in STEPS)

    def render(self) -> str:
        verdict = "PASS" if self.ok else "FAIL"
        lines = [f"AutoForge live Pi smoke test (#135): {verdict}"]
        lines += [f"  {key}: {value}" for key, value in self.facts.items()]
        for name in STEPS:
            if name not in self.outcomes:
                lines.append(f"  [NOT RUN] {name}")
                continue
            ok, message = self.outcomes[name]
            lines.append(f"  [{'PASS' if ok else 'FAIL'}] {name}" + ("" if ok else f": {message}"))
        return redact("\n".join(lines))


# -- the flow ----------------------------------------------------------------------
@dataclass(frozen=True)
class SmokeSettings:
    """What the run uses: ``command`` is the profile's (``pi`` on ``PATH``)."""

    command: str = "pi"
    model: str = DEFAULT_MODEL
    effort: str = DEFAULT_EFFORT


def settings_from_environment() -> SmokeSettings:
    return SmokeSettings(
        model=os.environ.get(MODEL_VARIABLE) or DEFAULT_MODEL,
        effort=os.environ.get(EFFORT_VARIABLE) or DEFAULT_EFFORT,
    )


class RecordingPi(PiProvider):
    """The real :class:`PiProvider`, keeping each result for the report."""

    def __init__(self) -> None:
        super().__init__()
        self.results: list[AgentExecutionResult] = []

    def execute(self, req: AgentRequest) -> AgentExecutionResult:
        result = super().execute(req)
        self.results.append(result)
        return result


def write_repository(root: Path) -> Path:
    """A git repository holding the feature specification, committed."""
    root.mkdir(parents=True)
    (root / FEATURE).parent.mkdir()
    (root / FEATURE).write_text(FEATURE_MD, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    identity = ["-c", "user.name=AutoForge smoke", "-c", "user.email=smoke@example.invalid"]
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], [*identity, "commit", "-qm", "x"]):
        subprocess.run(["git", "-C", str(root), *argv], check=True, capture_output=True)
    return root


def write_config(path: Path, settings: SmokeSettings) -> Path:
    """The run's configuration: the implementer and fixer scripted, every
    reviewer a LOCAL run can reach on Pi with ``require_oauth``, and no
    correction relaunch, so the review is one model call."""
    reviewer = {
        "provider": "pi",
        "model": settings.model,
        "effort": settings.effort,
        "command": settings.command,
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
        "max_runtime_seconds": MAX_RUNTIME_SECONDS,
        "options": {"require_oauth": True},
    }
    data = {
        "version": 1,
        "execution": {"max_correction_attempts": 0},
        "local": {"exclude": ["**/__pycache__"]},
        "profiles": {
            "analyze_execute": {"provider": "scripted"},
            "fix": {"provider": "scripted"},
            "review_round_1": reviewer,
            "review_round_2_5": reviewer,
        },
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def implement(root: Path) -> Callable[[AgentRequest], str]:
    """The scripted ANALYZE_EXECUTE: writes the feature, answers with its result."""
    from tests.conftest import block

    def handler(req: AgentRequest) -> str:
        (root / IMPLEMENTATION).write_text(IMPLEMENTATION_PY, encoding="utf-8")
        payload = {
            "phase": "ANALYZE_EXECUTE",
            "status": "success",
            "summary": "Added greet(name) in greet.py.",
            "changed_workspace": True,
            "tests_attempted": [],
        }
        return block(payload)

    return handler


def _pi_profile(settings: SmokeSettings) -> ProfileConfig:
    return ProfileConfig(
        name="review_round_1",
        provider="pi",
        model=settings.model,
        effort=settings.effort,
        command=settings.command,
        options={"require_oauth": "true"},
    )


def check_preconditions(settings: SmokeSettings, cwd: Path, report: SmokeReport) -> None:
    """Step 1: ``pi`` on PATH, its version, a ChatGPT sign-in (Pi's read-only
    ``auth check --no-refresh``). Pi's output is never shown."""
    path = shutil.which(settings.command)
    if path is None:
        raise StepFailed(f"`{settings.command}` is not on PATH: install Pi yourself (docs/pi.md)")
    version = subprocess.run(
        [path, "--version"], cwd=cwd, capture_output=True, text=True, timeout=PROBE_TIMEOUT_SECONDS
    )
    text = version.stdout.strip()
    found = parse_pi_version(text) if version.returncode == 0 else None
    minimum = ".".join(str(part) for part in PI_MIN_VERSION)
    if found is None:
        raise StepFailed(f"cannot read the version from `pi --version` (need >= {minimum})")
    release, _, suffix = text.partition("-")
    report.facts["pi"] = f"{release} (pre-release)" if suffix else release
    require(found >= PI_MIN_VERSION, f"pi {release}: pi >= {minimum} is required")
    profile = _pi_profile(settings)
    provider = PiProvider()
    auth = subprocess.run(
        [path, *provider.auth_check_command(profile)[1:]],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    ok, detail = provider.auth_verdict(profile, auth.returncode, auth.stdout)
    require(ok, f"not signed in for {settings.model}: {detail}")


def check_no_api_key() -> None:
    """Step 2: proven in the shell as it is, so never unset here."""
    require(
        "OPENAI_API_KEY" not in os.environ,
        "OPENAI_API_KEY is set: unset it for this test (it proves the subscription path, "
        "so it does not unset it for you)",
    )


def run_doctor(config: Path, repo: Path, settings: SmokeSettings, report: SmokeReport) -> str:
    """Step 3: ``autoforge local doctor --json``, the real CLI in its own
    process; returns what it printed, which step 5 scans too."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoforge.cli",
            "--config",
            str(config),
            "local",
            "doctor",
            "--json",
        ],
        cwd=repo,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    try:
        checks = json.loads(proc.stdout)["checks"]
    except (ValueError, KeyError, TypeError):
        raise StepFailed(f"doctor exited {proc.returncode} without its JSON report") from None
    available = f"agent '{settings.command}' available ("
    auth = f"agent '{settings.command}' auth ready ("
    pi_rows = [c for c in checks if c["name"].startswith((available, auth))]
    others = [c["name"] for c in checks if c["required"] and not c["ok"] and c not in pi_rows]
    if others:
        report.facts["doctor, other failures"] = "; ".join(others)
    for row in pi_rows:
        kind = "available" if row["name"].startswith(available) else "auth ready"
        report.facts[f"doctor, {kind}"] = row["detail"]
    rows = [c for c in pi_rows if c["name"].startswith(available)]
    require(rows and all(c["ok"] for c in rows), "doctor's 'available' row did not pass")
    rows = [c for c in pi_rows if c["name"].startswith(auth)]
    require(rows and all(c["ok"] for c in rows), "doctor's 'auth ready' row did not pass")
    require(
        all("authType oauth" in c["detail"] for c in rows),
        "doctor's 'auth ready' row does not name authType oauth",
    )
    return proc.stdout


def run_smoke(
    workdir: Path,
    settings: SmokeSettings,
    report: SmokeReport,
    before_review: Callable[[ControllerEngine], None] | None = None,
) -> None:
    """Steps 1 to 6 in ``workdir`` (empty, of this run alone); ``report`` holds
    the outcome. ``before_review`` lets the offline rehearsal arm its fake."""
    from tests.conftest import ExplodingGitHub

    report.facts["date (UTC)"] = datetime.datetime.now(datetime.UTC).date().isoformat()
    report.facts["model"] = settings.model
    report.facts["thinking"] = settings.effort
    sessions = pi_session_dir()
    sessions_before = entry_names(sessions)
    repo = write_repository(workdir / "repo")
    config = write_config(workdir / "autoforge.json", settings)
    doctor_output = ""

    with report.step(STEPS[0]):
        check_preconditions(settings, workdir, report)
    with report.step(STEPS[1]):
        check_no_api_key()
    with report.step(STEPS[2]):
        doctor_output = run_doctor(config, repo, settings, report)
    if not all(report.passed(name) for name in STEPS[:3]):
        return

    pi = RecordingPi()
    tree_before: dict[str, str] | None = None
    with report.step(STEPS[3]):
        registry = ProviderRegistry(
            overrides={"scripted": ScriptedProvider(implement(repo)), "pi": pi}
        )
        engine = ControllerEngine(
            config=load_config_file(str(config)),
            state_dir=None,
            workdir=repo,
            github=ExplodingGitHub(),  # type: ignore[arg-type]
            providers=registry,
        )
        engine.bind_local_state_dir(None)
        with engine.locked():
            engine.new_local_run(FEATURE)
            engine.validate_config()
            engine.save()
            engine.step()  # INITIALIZING -> ANALYZE_EXECUTE
            engine.step()  # ANALYZE_EXECUTE (scripted) -> REVIEW
            require(engine.state.phase is Phase.REVIEW, f"no REVIEW: {engine.state.phase.value}")
            tree_before = tree_snapshot(repo)
            if before_review is not None:
                before_review(engine)
            started = time.monotonic()
            try:
                engine.step()  # REVIEW on Pi
            except Exception as exc:
                raise StepFailed(_review_failure(pi, exc)) from None
            finally:
                report.facts["review"] = _review_facts(pi, time.monotonic() - started)
        state = engine.state
        require(len(pi.results) == 1, f"{len(pi.results)} Pi launches, not one")
        require(
            state.phase in (Phase.DONE, Phase.FIX),
            f"the review did not complete: the run is {state.phase.value}",
        )
        findings = len(state.open_findings)
        report.facts["review outcome"] = (
            "clean, the run is DONE"
            if state.phase is Phase.DONE
            else f"{findings} finding(s), the run would FIX next"
        )

    with report.step(STEPS[4]):
        hits = scan_for_credentials(repo / ".git" / "autoforge")
        hits += [("<doctor output>", name) for name in credential_hits(doctor_output)]
        require(not hits, "; ".join(f"{path}: {name}" for path, name in hits))

    with report.step(STEPS[5]):
        problems = []
        if tree_before is None:
            problems.append("the review never started, so the tree was not compared")
        elif tree_snapshot(repo) != tree_before:
            problems.append("the review changed the working tree")
        added = entry_names(sessions) - sessions_before
        if added:
            problems.append(f"{len(added)} new entr(ies) under {sessions}")
        left = processes_under(workdir)
        if left:
            problems.append(f"process(es) {', '.join(map(str, left))} still run under {workdir}")
        for result in pi.results:
            if result.group_survived_kill or result.orphan_survived_kill:
                problems.append("a process of the Pi launch survived its kill")
            if result.orphans_unchecked:
                problems.append("this platform cannot contain Pi's detached tool processes")
        require(not problems, "; ".join(problems))


def _review_failure(pi: RecordingPi, exc: Exception) -> str:
    """Why the review step raised: #131's category for a Pi failure, the
    controller's own error otherwise. Never Pi's or the model's text."""
    result = pi.results[-1] if pi.results else None
    if result is not None and result.timed_out:
        return f"timed out ({result.timeout_limit or 'limit'})"
    if result is not None and result.provider_failure:
        return classify_failure(result.provider_failure)
    message = str(exc).split(". stderr tail:", 1)[0]
    return f"AutoForge failure ({type(exc).__name__}: {_first_line(message)})"


def _review_facts(pi: RecordingPi, seconds: float) -> str:
    if not pi.results:
        return f"no Pi launch ({seconds:.1f} s)"
    result = pi.results[-1]
    summary = result.provider_summary or {}
    resolved = f"{summary.get('resolved_provider', '?')}/{summary.get('resolved_model_id', '?')}"
    return (
        f"{seconds:.1f} s; Pi resolved {resolved} at thinking "
        f"{summary.get('resolved_thinking', '?')}; stop reason "
        f"{summary.get('stop_reason') or '-'}; {summary.get('tool_executions', 0)} tool call(s); "
        f"{summary.get('auto_retries', 0)} auto-retr(ies)"
    )
