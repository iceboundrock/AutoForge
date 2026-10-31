"""The live Pi smoke test (#135): its flow, its checks and its report.

``tests/test_live_pi.py`` runs :func:`run_smoke` against the real ``pi`` and
a credential the operator gave Pi beforehand; ``tests/test_live_pi_support.py``
runs the same flow against the fake ``pi`` of ``tests/pi_fake.py``, offline,
and tests each check on its own. A run takes one of two auth routes, named
explicitly (``AUTOFORGE_LIVE_PI_AUTH``): ``api_key``, a provider API key with
``require_oauth: false`` (DeepSeek first, the acceptance run of #135's
amendment of 2026-10-20), or ``oauth``, the ChatGPT sign-in with
``require_oauth: true`` (an optional regression run). One run is seven
steps (docs/development.md, "The live Pi smoke test"):

1. preconditions, checked and never performed: ``pi`` is installed, at
   least :data:`~autoforge.providers.PI_MIN_VERSION`, and Pi's read-only
   auth check resolves the model to a credential of the route's type;
2. no provider API key (:data:`PROVIDER_KEY_NAMES`) is in this process's
   environment, unless the operator forwards one on purpose
   (``AUTOFORGE_LIVE_PI_FORWARD_KEY``, the ``api_key`` route only);
3. ``autoforge local doctor`` (the real CLI, in a process of its own) passes
   its Pi rows: Pi available, auth ready with the route's ``authType``;
4. one LOCAL REVIEW on Pi returns a valid ``CONTROL_RESULT``: a scripted
   stand-in implements a one-line feature, so the review is the only model
   call (``execution.max_correction_attempts: 0``);
5. nothing AutoForge wrote holds a credential-shaped string;
6. the review left no side effect AutoForge promised against: the tree is
   unchanged, Pi's session directory gained no entry, no process is left;
7. the report: what ran (the AutoForge commit, the route) and each step's
   outcome.

What this never does: start ``/login`` or Pi interactively, read a file in
Pi's agent directory (``auth.json`` included; the session check lists names
only), pass ``--credentials`` or a ``print-*`` subcommand, or print a key, a
token, an account id, an email or a plan name. A model's or Pi's own error
text is not printed either, since it may name the plan: a failure is
reported by its category (#131's failure reasons), never by its detail.

**The REMOTE flow** (:func:`run_remote_smoke`, ADR 0009 Appendix A.2.3 G):
LOCAL mode is being removed (EPIC #383), and with it ``local doctor``, the
``local:`` key and the LOCAL REVIEW above. The REMOTE flow runs the same
seven steps on a REMOTE run, so that Pi keeps a live flow and offline
rehearsals once the LOCAL one is gone. Steps 1, 2 and 7 are the same.
The others:

3. ``autoforge doctor --json``, the real CLI in a process of its own,
   passes its Pi rows. The rows that need GitHub (``gh`` available and
   authenticated, the default branch's required checks, the check runs read)
   cannot pass without the operator's GitHub login, and this test uses none:
   the configuration's ``github.command`` is the stand-in's ``gh``, which
   exits 1 and reaches nothing. Those rows fail or skip by construction and
   are reported as a fact. The Pi rows do not depend on them: doctor derives
   them from the configuration and Pi alone, as for LOCAL. The real CLI is
   preferred to an in-process ``Doctor`` restricted to the Pi rows because it
   is what the operator runs, with the REMOTE set of reachable profiles, in
   a process the test's own stand-ins do not reach.
4. one REMOTE REVIEW on Pi: a scripted stand-in implements the one-line
   feature as a commit in the per-issue worktree, the controller pushes it
   and opens the PR, then the REVIEW of that PR HEAD on Pi is the only model
   call and must return a valid ``CONTROL_RESULT``. The controller posts the
   round's comment itself.
5. the scan covers the state directory (outside the checkout), the git
   directory's ``autoforge/`` (the lock and the per-issue worktree),
   every request the controller sent to the GitHub stand-in (the PR's title
   and body, the review comment), and doctor's output.
6. the review left the checkout, its worktree and the worktree's HEAD
   unchanged; the session and process checks are LOCAL's.

**The GitHub stand-in** (the design question A.2.3 G leaves to S1a) is the
suite's in-memory ``FakeGitHub`` (``tests/conftest.py``), its git data in a
bare repository under the run's directory that the controller's own git
transport pushes to over ``file://``, as the engine tests' ``origin=True``
does. The test seeds that origin with the suite's isolated git, over no
transport: it writes into the bare repository alone, whatever URL rewrite,
credential helper or SSH command the operator's git configuration or
environment holds (#386 R1-F1). A scratch repository on github.com was the
alternative. It was rejected because it would need the operator's ``gh``
login and the network, leave a branch, a PR and a comment to clean up, and
break the promise that the live test writes nothing to GitHub
(``tests/AGENTS.md``), all to prove what this test is not about: the test
proves Pi on a REMOTE REVIEW, and the GitHub side of REMOTE is covered
offline by the suite against the same fake. The cost: what the controller
sent to GitHub is checked against the fake's model of GitHub, not GitHub's,
and the run says nothing about ``gh``.
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
from typing import TYPE_CHECKING

import autoforge
from autoforge.config import ProfileConfig, load_config_file
from autoforge.doctor import CHECKS_READ_ROW, REQUIRED_CHECKS_ROW
from autoforge.engine import ControllerEngine
from autoforge.git_transport import NO_AUTO_MAINTENANCE
from autoforge.providers import (
    PI_DEFAULT_AGENT_DIR,
    PI_MIN_VERSION,
    AgentExecutionResult,
    AgentRequest,
    PiProvider,
    ProviderRegistry,
    ScriptedProvider,
    parse_pi_auth_check,
    parse_pi_version,
)
from autoforge.redaction import redact
from autoforge.transitions import Phase

if TYPE_CHECKING:
    from tests.conftest import FakeGitHub

# `AUTOFORGE_LIVE_PI=1` (and `-m live`) runs the smoke test; any other
# non-empty value is refused rather than read as "no".
LIVE_VARIABLE = "AUTOFORGE_LIVE_PI"
# The auth route, `api_key` or `oauth`, and the `authType` Pi's auth check
# must report for it. Never defaulted, so a report always says which route
# it proves: a run on an API key is no evidence for the ChatGPT sign-in.
AUTH_VARIABLE = "AUTOFORGE_LIVE_PI_AUTH"
AUTH_ROUTES = ("api_key", "oauth")
# The model and thinking level of the review: an account may not have the
# default model, and a non-reasoning model needs `off`. DeepSeek's
# `deepseek-flash` takes `low`, `high` and `max` without a clamp.
MODEL_VARIABLE = "AUTOFORGE_LIVE_PI_MODEL"
EFFORT_VARIABLE = "AUTOFORGE_LIVE_PI_EFFORT"
DEFAULT_MODELS = {"api_key": "deepseek/deepseek-flash", "oauth": "openai/gpt-5.6-terra"}
DEFAULT_EFFORT = "low"
# The provider API keys step 2 requires absent from this process, so that
# the credential reaches Pi only through Pi's own store.
PROVIDER_KEY_NAMES = ("OPENAI_API_KEY", "DEEPSEEK_API_KEY")
# A key the operator forwards on purpose (the `api_key` route only): its
# name goes to `execution.env_allowlist_extra`, which every launch gets.
FORWARD_VARIABLE = "AUTOFORGE_LIVE_PI_FORWARD_KEY"
_KEY_NAME_RE = re.compile(r"[A-Z][A-Z0-9_]*_API_KEY")
# The review's limits: it reads two small files.
IDLE_TIMEOUT_SECONDS = 300
MAX_RUNTIME_SECONDS = 900
# Pi's own read-only commands (`--version`, `auth check`) and doctor.
PROBE_TIMEOUT_SECONDS = 120

STEPS = (
    "1. preconditions: pi installed, recent enough, a credential of the route's type",
    "2. no provider API key in AutoForge's environment, unless forwarded on purpose",
    "3. doctor: pi available, pi auth ready with the route's authType",
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

# The REMOTE flow's run, as the report names it.
REMOTE_MODE = (
    "REMOTE, against a GitHub stand-in: the in-memory FakeGitHub, its git data in a bare "
    "repository under the run's directory; nothing reaches GitHub"
)
# The doctor rows that need GitHub. The stand-in's `gh` answers nothing, so
# in the REMOTE flow they fail or skip by construction: reported as a fact,
# never counted against step 3.
DOCTOR_GITHUB_ROWS = ("gh available", "gh authenticated", REQUIRED_CHECKS_ROW, CHECKS_READ_ROW)


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


def credential_hits(text: str, values: Mapping[str, str] | None = None) -> list[str]:
    """The names of the patterns ``text`` matches, in pattern order, then
    ``the value of NAME`` for each of ``values`` (name to value: a key
    forwarded on purpose, whatever its shape) it holds; never a match."""
    hits = [name for name, pattern in CREDENTIAL_PATTERNS if pattern.search(text)]
    hits += [f"the value of {name}" for name, value in (values or {}).items() if value in text]
    return hits


def scan_for_credentials(
    root: Path, values: Mapping[str, str] | None = None
) -> list[tuple[str, str]]:
    """``(path relative to root, pattern name)`` for each credential-shaped
    string in a file under ``root``, and each of ``values`` it holds
    (:func:`credential_hits`). Symbolic links are not followed; the matched
    text is never returned."""
    hits: list[tuple[str, str]] = []
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        for name in sorted(files):
            path = Path(directory) / name
            if path.is_symlink() or not path.is_file():
                continue
            text = path.read_bytes().decode("utf-8", errors="replace")
            relative = str(path.relative_to(root))
            hits.extend((relative, hit) for hit in credential_hits(text, values))
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
    fingerprint binds, and in the REMOTE flow the only look at the checkout
    and the reviewer's worktree (a linked worktree's ``.git`` is a file, and
    is compared like one)."""
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
    """What the run uses: ``command`` is the profile's (``pi`` on ``PATH``),
    ``auth`` the route (:data:`AUTH_ROUTES`), ``forward_key`` the name of a
    key forwarded on purpose, or ''."""

    command: str = "pi"
    auth: str = "api_key"
    model: str = DEFAULT_MODELS["api_key"]
    effort: str = DEFAULT_EFFORT
    forward_key: str = ""

    @property
    def require_oauth(self) -> bool:
        return self.auth == "oauth"


def settings_from_environment(env: Mapping[str, str] = os.environ) -> SmokeSettings:
    """The settings the operator chose; :class:`ValueError` names a wrong one."""
    auth = env.get(AUTH_VARIABLE, "")
    if auth not in AUTH_ROUTES:
        raise ValueError(
            f"set {AUTH_VARIABLE} to api_key (a provider API key, require_oauth: false; "
            f"default model {DEFAULT_MODELS['api_key']}) or oauth (the ChatGPT sign-in, "
            f"require_oauth: true; default model {DEFAULT_MODELS['oauth']}), got {auth!r}"
        )
    forward = env.get(FORWARD_VARIABLE, "")
    if forward and auth != "api_key":
        raise ValueError(f"{FORWARD_VARIABLE} forwards an API key: the oauth route takes none")
    if forward and not _KEY_NAME_RE.fullmatch(forward):
        raise ValueError(f"{FORWARD_VARIABLE} must name a provider key (NAME_API_KEY)")
    return SmokeSettings(
        auth=auth,
        model=env.get(MODEL_VARIABLE) or DEFAULT_MODELS[auth],
        effort=env.get(EFFORT_VARIABLE) or DEFAULT_EFFORT,
        forward_key=forward,
    )


def forwarded_values(settings: SmokeSettings) -> dict[str, str]:
    """The forwarded key's value by name, for step 5's scan; never shown."""
    value = os.environ.get(settings.forward_key, "") if settings.forward_key else ""
    return {settings.forward_key: value} if value else {}


def autoforge_commit() -> str:
    """The AutoForge commit under test, from the checkout ``autoforge`` is
    imported from, and whether its tracked files differ from it."""
    package = Path(autoforge.__file__).resolve().parent

    def git(*argv: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", str(package), *argv],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
        )

    found = git("rev-parse", "--show-toplevel", "HEAD")
    lines = found.stdout.split()
    if found.returncode != 0 or len(lines) != 2 or Path(lines[0]) / "src" / "autoforge" != package:
        return "unknown (autoforge is not imported from a git checkout)"
    dirty = git("status", "--porcelain", "--untracked-files=no").stdout.strip()
    return lines[1] + (" with uncommitted changes" if dirty else "")


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
    """A git repository holding the feature specification, committed by
    the suite's isolated git: none of the operator's git configuration,
    hooks or ``GIT_*`` variables apply (#386 R1-F1)."""
    from tests.conftest import isolated_git

    root.mkdir(parents=True)
    (root / FEATURE).parent.mkdir()
    (root / FEATURE).write_text(FEATURE_MD, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    identity = ["-c", "user.name=AutoForge smoke", "-c", "user.email=smoke@example.invalid"]
    for argv in (["init", "-q", "-b", "main"], ["add", "-A"], [*identity, "commit", "-qm", "x"]):
        isolated_git("-C", str(root), *argv)
    return root


def _reviewer(settings: SmokeSettings) -> dict[str, object]:
    """A reviewer profile on Pi with the route's ``require_oauth``."""
    return {
        "provider": "pi",
        "model": settings.model,
        "effort": settings.effort,
        "command": settings.command,
        "idle_timeout_seconds": IDLE_TIMEOUT_SECONDS,
        "max_runtime_seconds": MAX_RUNTIME_SECONDS,
        "options": {"require_oauth": settings.require_oauth},
    }


def _execution(settings: SmokeSettings) -> dict[str, object]:
    """No correction relaunch, so the review is one model call; a forwarded
    key in ``execution.env_allowlist_extra``."""
    execution: dict[str, object] = {"max_correction_attempts": 0}
    if settings.forward_key:
        execution["env_allowlist_extra"] = [settings.forward_key]
    return execution


def write_config(path: Path, settings: SmokeSettings) -> Path:
    """The run's configuration: the implementer and fixer scripted, every
    reviewer a LOCAL run can reach on Pi with the route's ``require_oauth``,
    a forwarded key in ``execution.env_allowlist_extra``, and no correction
    relaunch, so the review is one model call."""
    reviewer = _reviewer(settings)
    execution = _execution(settings)
    data = {
        "version": 1,
        "execution": execution,
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


def write_remote_config(path: Path, settings: SmokeSettings, gh: Path, state_dir: Path) -> Path:
    """The REMOTE run's configuration: every profile a REMOTE run can reach
    (``REQUIRED_PROFILES`` and ``update_epic``) scripted except the three
    reviewers, on Pi with the route's ``require_oauth``; ``gh``, the GitHub
    stand-in's, for every ``gh`` doctor or the controller runs; the state
    outside the checkout (``state_dir``), so that the checkout holds nothing
    AutoForge writes; and LOCAL's ``execution``."""
    reviewer = _reviewer(settings)
    scripted = {"provider": "scripted"}
    data = {
        "version": 1,
        "state_dir": str(state_dir),
        "github": {"command": str(gh)},
        "execution": _execution(settings),
        "profiles": {
            "analyze_execute": scripted,
            "fix": scripted,
            "replan_reexecute": scripted,
            "update_epic": scripted,
            "review_round_1": reviewer,
            "review_round_2_5": reviewer,
            "review_round_6_plus": reviewer,
        },
    }
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    return path


def implement_in_worktree(req: AgentRequest) -> str:
    """The scripted REMOTE ANALYZE_EXECUTE: commits the feature in its
    worktree (the controller pushes it and opens the PR) and reports it."""
    from tests.conftest import GIT_IDENT, analyze_payload, block, git_out

    worktree = str(req.cwd)
    (Path(worktree) / IMPLEMENTATION).write_text(IMPLEMENTATION_PY, encoding="utf-8")
    git_out("-C", worktree, "add", IMPLEMENTATION)
    git_out(
        "-C", worktree, *NO_AUTO_MAINTENANCE, *GIT_IDENT, "commit", "-qm", "Add greet(name) (#2)"
    )
    payload = analyze_payload(
        git_out("-C", worktree, "rev-parse", "HEAD"),
        pr_title="Add greet(name)",
        pr_body="Adds `greet(name)` in `greet.py`.",
        tests=[],
    )
    return block(payload)


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
        options={"require_oauth": "true" if settings.require_oauth else "false"},
    )


def check_preconditions(settings: SmokeSettings, cwd: Path, report: SmokeReport) -> None:
    """Step 1: ``pi`` on PATH, its version, and Pi's read-only ``auth check
    --no-refresh`` resolving the model to a credential of the route's
    ``authType``; Pi 1.3.0 answers an id it does not resolve ``invalid``
    (an older Pi may not, and step 4's launch checks the model again).
    Run without a provider key step 2 would refuse, so it sees what the
    launch would. Pi's output is never shown."""
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
    env = {
        name: value
        for name, value in os.environ.items()
        if name not in PROVIDER_KEY_NAMES or name == settings.forward_key
    }
    auth = subprocess.run(
        [path, *provider.auth_check_command(profile)[1:]],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    ok, detail = provider.auth_verdict(profile, auth.returncode, auth.stdout)
    require(ok, f"no usable credential for {settings.model}: {detail}")
    check = parse_pi_auth_check(auth.stdout, auth.returncode)
    report.facts["auth"] = f"Pi resolved authType {check.auth_type} for provider {check.provider!r}"
    require(
        check.auth_type == settings.auth,
        f"Pi resolved authType {check.auth_type} for {settings.model}, "
        f"not the {settings.auth} route's",
    )


def check_no_api_key(settings: SmokeSettings, report: SmokeReport) -> None:
    """Step 2: proven in the shell as it is, so never unset here. Only a key
    forwarded on purpose may be set, and it then must be; its name is
    reported, never its value."""
    present = [n for n in PROVIDER_KEY_NAMES if os.environ.get(n) and n != settings.forward_key]
    why = (
        "it proves the subscription path"
        if settings.require_oauth
        else f"the key reaches Pi through Pi's own store, or forward one on purpose with "
        f"{FORWARD_VARIABLE}"
    )
    require(
        not present,
        f"{', '.join(present)} is set: unset it for this test, which does not unset it "
        f"for you ({why})",
    )
    if not settings.forward_key:
        report.facts["credential"] = "Pi's own credential store (no provider key forwarded)"
        return
    require(
        os.environ.get(settings.forward_key),
        f"{FORWARD_VARIABLE} names {settings.forward_key}, which is not set",
    )
    report.facts["credential"] = (
        f"{settings.forward_key} forwarded on purpose by execution.env_allowlist_extra "
        "(its value is not shown; Pi prefers a stored credential when it holds one)"
    )


def run_doctor(
    config: Path,
    repo: Path,
    settings: SmokeSettings,
    report: SmokeReport,
    subcommand: tuple[str, ...] = ("local", "doctor"),
) -> str:
    """Step 3: ``autoforge local doctor --json`` (or ``subcommand``: the
    REMOTE flow's ``doctor``), the real CLI in its own process; returns what
    it printed, which step 5 scans too. A row that needs GitHub
    (:data:`DOCTOR_GITHUB_ROWS`, which ``local doctor`` has none of) is
    reported apart from the other failures."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "autoforge.cli",
            "--config",
            str(config),
            *subcommand,
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
    failed = [c["name"] for c in checks if c["required"] and not c["ok"] and c not in pi_rows]
    github = [name for name in failed if name in DOCTOR_GITHUB_ROWS]
    others = [name for name in failed if name not in DOCTOR_GITHUB_ROWS]
    if github:
        report.facts["doctor, GitHub rows"] = (
            f"failed, as expected: the stand-in's gh answers nothing ({'; '.join(github)})"
        )
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
        all(f"authType {settings.auth}" in c["detail"] for c in rows),
        f"doctor's 'auth ready' row does not name authType {settings.auth}",
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

    _run_facts(settings, report)
    sessions = pi_session_dir()
    sessions_before = entry_names(sessions)
    repo = write_repository(workdir / "repo")
    config = write_config(workdir / "autoforge.json", settings)
    doctor_output = ""

    with report.step(STEPS[0]):
        check_preconditions(settings, workdir, report)
    with report.step(STEPS[1]):
        check_no_api_key(settings, report)
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
        values = forwarded_values(settings)
        hits = scan_for_credentials(repo / ".git" / "autoforge", values)
        hits += [("<doctor output>", name) for name in credential_hits(doctor_output, values)]
        require(not hits, "; ".join(f"{path}: {name}" for path, name in hits))

    with report.step(STEPS[5]):
        problems = []
        if tree_before is None:
            problems.append("the review never started, so the tree was not compared")
        elif tree_snapshot(repo) != tree_before:
            problems.append("the review changed the working tree")
        problems += _launch_side_effects(sessions, sessions_before, workdir, pi)
        require(not problems, "; ".join(problems))


def _run_facts(settings: SmokeSettings, report: SmokeReport) -> None:
    """Step 7's facts of the run that both flows report first."""
    report.facts["date (UTC)"] = datetime.datetime.now(datetime.UTC).date().isoformat()
    report.facts["AutoForge commit"] = autoforge_commit()
    report.facts["auth route"] = (
        f"{settings.auth} (require_oauth: {str(settings.require_oauth).lower()})"
    )
    report.facts["model"] = settings.model
    report.facts["thinking"] = settings.effort


def _launch_side_effects(
    sessions: Path, sessions_before: frozenset[str], workdir: Path, pi: RecordingPi
) -> list[str]:
    """Step 6's checks of the Pi launch itself: a new session entry, a process
    left in or naming ``workdir``, a process that survived its kill."""
    problems = []
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
    return problems


# -- the REMOTE flow (ADR 0009 Appendix A.2.3 G) --------------------------------------
def worktree_head(worktree: Path) -> str:
    """The HEAD of the per-issue worktree, or '' when it cannot be read."""
    found = subprocess.run(
        ["git", "-C", str(worktree), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        timeout=PROBE_TIMEOUT_SECONDS,
    )
    return found.stdout.strip() if found.returncode == 0 else ""


def remote_trees(checkout: Path, worktree: Path) -> dict[str, object]:
    """What a REMOTE review must leave as it found it: the operator's
    checkout, the per-issue worktree the reviewer runs in, and its HEAD."""
    return {
        "checkout": tree_snapshot(checkout),
        "worktree": tree_snapshot(worktree),
        "worktree HEAD": worktree_head(worktree),
    }


# What each difference of :func:`remote_trees` means, in its order.
REMOTE_TREE_CHANGES = {
    "checkout": "the review changed the checkout",
    "worktree": "the review changed its worktree",
    "worktree HEAD": "the review moved its worktree's HEAD",
}


def stand_in_requests(github: FakeGitHub) -> list[tuple[str, str]]:
    """``(label, text)`` for every request the controller sent to the GitHub
    stand-in, in order: a write's title and body, a read's URL."""
    return [
        (f"<GitHub stand-in: {call[0]}>", "\n".join(str(part) for part in call[1:]))
        for call in github.calls
    ]


def run_remote_smoke(
    workdir: Path,
    settings: SmokeSettings,
    report: SmokeReport,
    before_review: Callable[[ControllerEngine], None] | None = None,
) -> None:
    """Steps 1 to 6 of the REMOTE flow in ``workdir`` (empty, of this run
    alone); ``report`` holds the outcome. ``before_review`` lets the offline
    rehearsal arm its fake."""
    from tests.conftest import EPIC, ISSUE, FakeGitHub, Origin, connect_origin, github_origin

    report.facts["mode"] = REMOTE_MODE
    _run_facts(settings, report)
    sessions = pi_session_dir()
    sessions_before = entry_names(sessions)
    repo = github_origin(write_repository(workdir / "repo"))
    origin = Origin.seeded(workdir, repo)
    state_dir = workdir / "state"
    config = write_remote_config(workdir / "autoforge.json", settings, origin.gh, state_dir)
    github = FakeGitHub()
    github.issues[ISSUE].body = FEATURE_MD
    github.attach_origin(origin)
    doctor_output = ""

    with report.step(STEPS[0]):
        check_preconditions(settings, workdir, report)
    with report.step(STEPS[1]):
        check_no_api_key(settings, report)
    with report.step(STEPS[2]):
        doctor_output = run_doctor(config, repo, settings, report, ("doctor",))
    if not all(report.passed(name) for name in STEPS[:3]):
        return

    pi = RecordingPi()
    worktree: Path | None = None
    trees_before: dict[str, object] | None = None
    with report.step(STEPS[3]):
        registry = ProviderRegistry(
            overrides={"scripted": ScriptedProvider(implement_in_worktree), "pi": pi}
        )
        engine = ControllerEngine(
            config=load_config_file(str(config)),
            state_dir=None,
            workdir=repo,
            github=github,  # type: ignore[arg-type]
            providers=registry,
        )
        connect_origin(engine, origin)
        engine.validate_config()
        with engine.locked():
            engine.new_run(EPIC, ISSUE)
            engine.record_run_configuration()
            engine.save()
            engine.step()  # INITIALIZING -> ANALYZE_EXECUTE
            engine.step()  # ANALYZE_EXECUTE (scripted): pushed, the PR opened -> REVIEW
            require(engine.state.phase is Phase.REVIEW, f"no REVIEW: {engine.state.phase.value}")
            worktree = engine.agent_worktree_path()
            trees_before = remote_trees(repo, worktree)
            if before_review is not None:
                before_review(engine)
            started = time.monotonic()
            try:
                engine.step()  # REVIEW on Pi
            except Exception as exc:
                raise StepFailed(_review_failure(pi, exc)) from None
            finally:
                report.facts["review"] = _review_facts(pi, time.monotonic() - started)
                writes = [name for name, _, _ in github.effect_writes]
                report.facts["GitHub stand-in, the controller's writes"] = (
                    ", ".join(writes) or "(none)"
                )
        state = engine.state
        require(len(pi.results) == 1, f"{len(pi.results)} Pi launches, not one")
        require(
            state.phase in (Phase.READY_FOR_MERGE, Phase.FIX),
            f"the review did not complete: the run is {state.phase.value}",
        )
        findings = len(state.open_findings)
        report.facts["review outcome"] = (
            "clean, the run is READY_FOR_MERGE (nothing is merged: the merge gate is closed)"
            if state.phase is Phase.READY_FOR_MERGE
            else f"{findings} finding(s), the run would FIX next"
        )

    with report.step(STEPS[4]):
        values = forwarded_values(settings)
        hits = scan_for_credentials(state_dir, values)
        hits += [
            (str(Path(".git") / "autoforge" / path), name)
            for path, name in scan_for_credentials(repo / ".git" / "autoforge", values)
        ]
        for label, text in stand_in_requests(github):
            hits += [(label, name) for name in credential_hits(text, values)]
        hits += [("<doctor output>", name) for name in credential_hits(doctor_output, values)]
        found = list(dict.fromkeys(hits))
        require(not found, "; ".join(f"{path}: {name}" for path, name in found))

    with report.step(STEPS[5]):
        problems = []
        if worktree is None or trees_before is None:
            problems.append("the review never started, so the trees were not compared")
        else:
            after = remote_trees(repo, worktree)
            problems += [
                message
                for key, message in REMOTE_TREE_CHANGES.items()
                if after[key] != trees_before[key]
            ]
        problems += _launch_side_effects(sessions, sessions_before, workdir, pi)
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
