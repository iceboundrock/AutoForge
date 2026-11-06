"""The evaluation protocol's harness: scoring, decisions and reports (#318).

No test: the tool behind ``docs/evaluation-protocol.md``, which pre-registers
whether Issue Memory and multi-agent execution earn their cost. It launches no
agent and holds no credential. A run is either a fixture run, which
``tests/evaluation_fixture.py`` drives on fakes, or a real run an operator made
with the ordinary ``autoforge`` command on a disposable repository, after a
separate approval. This module reads what either left behind, read only, the
same way::

    uv run python -m tests.evaluation check
    uv run python -m tests.evaluation plan --comparison memory_benefit
    uv run python -m tests.evaluation verify-pins --git . --run-oracle --reference-suite
    AUTOFORGE_EVAL_REAL=1 uv run python -m tests.evaluation collect ...
    uv run python -m tests.evaluation report RECORD.json ... [--json]

**What counts.** A run succeeded only on typed evidence: the controller's
``ValidationRecord``\\ s and ``ReviewCertificate`` for the exact final candidate
(FULL evidence, ADR 0010 D1 and D2) and an independent oracle's result on that
commit, its tests hidden from the agent. An agent's claim, a log's prose and a
FAST run are never read. ``validation.commands`` being empty is its own status,
``not_configured``, and never a pass.

**What a report separates.** Fixture runs are harness correctness; only real
runs reach ``model_quality``, and only ``measured`` values reach a decision:
an ``estimated``, ``synthetic`` or ``unknown`` one leaves its criterion
``unknown``, which rules a ``GO`` out. A missing feature leaves its arm
``not_evaluated``. Failures, interventions, exclusions and unknown usage are
reported with every summary.

**What the oracle may run.** The candidate's own code, so it runs as
``validation.commands`` does (:func:`run_oracle`): a byte-exact export, the
allow-listed environment, the credential preflight and, under
``sandbox.mode: required``, the export-command sandbox. A refusal is
``not_run``, never a pass; neither it nor a ``timeout`` is a failure either, so
what turns on one is ``unknown`` (:func:`oracle_counts`).

**What a run's own records must say.** A record states settings the run's state
file and run log show it ran under, and a setting they do not hold is ``unknown``
(:func:`settings_evidence`). Evidence the run log cannot back, or a line of it that
is another run's or malformed, is ``unknown`` wherever it would have counted
(:func:`run_log_problems`, :func:`admitted_lines`). Neither is ever assumed from what
an operator passed, and a pathname says nothing of what a file held.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import math
import os
import random
import re
import shutil
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autoforge.certificate import Configured, certificate_problem
from autoforge.child_env import (
    isolated_overrides,
    operator_home,
    overrides_directory,
    preflight,
)
from autoforge.config import AutoForgeConfig, load_config_file
from autoforge.config_fingerprint import (
    FINGERPRINT_PROFILES,
    SOURCE_CREATED,
    SOURCE_UPGRADED,
    ProfileRouting,
    RecordedConfiguration,
    fingerprint_routing,
)
from autoforge.errors import AutoForgeError
from autoforge.evidence import (
    NO_SANDBOX_TEMPLATE,
    Outcome,
    Policy,
    applicable,
    definitions,
    parse_candidate,
)
from autoforge.executor import ExecutionRequest, execute, select_environment
from autoforge.git_transport import local_git_request
from autoforge.materialize import METHOD as MATERIALIZATION_METHOD
from autoforge.premerge import export_commit_tree
from autoforge.read_context import INDEX_FILE
from autoforge.runlog import STEP_DIR_RE
from autoforge.safefs import SafeRoot
from autoforge.sandbox_pin import SandboxPin
from autoforge.sandbox_policy import (
    LaunchClass,
    LaunchFacts,
    NetworkPosition,
    absolute_spelling,
    describe_launch,
    launch_template,
    template_facts,
)
from autoforge.state import AutoForgeState, load_state
from autoforge.transitions import Phase
from tests.stop_corpus import (
    _journal_bytes,
    _open_dir,
    bookkeeping_kind,
    launch_kind,
    operator_kind,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "evaluation"
MANIFEST = FIXTURES / "manifest.json"
PREREGISTRATION = FIXTURES / "preregistration.json"
REAL_RUN_CONFIG = FIXTURES / "real_run_config.yaml"
PIN_VERIFICATION = FIXTURES / "pin_verification.json"

MANIFEST_SCHEMA = "autoforge.evaluation-manifest/1"
PREREGISTRATION_SCHEMA = "autoforge.evaluation-preregistration/1"
RUN_SCHEMA = "autoforge.evaluation-run/1"
REPORT_SCHEMA = "autoforge.evaluation-report/1"
# What `collect` asks for, and no workflow in this repository sets
# (tests/test_evaluation.py): scoring a real run is the operator's act.
OPT_IN = "AUTOFORGE_EVAL_REAL"

FIXTURE, REAL = "fixture", "real"
MEASURED, ESTIMATED, SYNTHETIC, UNKNOWN = "measured", "estimated", "synthetic", "unknown"
NOT_APPLICABLE = "not_applicable"
PROVENANCES = (MEASURED, ESTIMATED, SYNTHETIC, UNKNOWN, NOT_APPLICABLE)

VERIFIED = "verified_success"
UNVERIFIED = "unverified_completion"
# Who stopped a run that never reached a verdict of its own. A pair with one of
# these leaves the denominator, listed and bounded (`max_excluded_pair_share`);
# anything else stays in it as a run that did not succeed.
EXCLUDED_OUTCOMES = ("infra_failure", "credentials_unavailable", "service_unavailable", "skipped")
# A run an operator stopped at a registered ceiling. It stays in the denominator
# as a run that did not succeed, and leaves its comparison incomplete.
CEILING_OUTCOMES = ("cost_ceiling", "wall_clock_ceiling")
STOPPED_BY = (*EXCLUDED_OUTCOMES, *CEILING_OUTCOMES)
OUTCOMES = (VERIFIED, UNVERIFIED, "blocked", "failed", "timeout", "incomplete", *STOPPED_BY)

# What an oracle run can say of a commit. ``timeout`` and ``not_run`` say neither: the
# hidden tests did not finish, or did not start.
ORACLE_RESULTS = ("pass", "fail")
# A completed run's reasons when all that stands between it and a success is a check
# that gave no result (score_run): nothing refuted it, and nothing verified it.
NO_RESULT_REASONS = frozenset(
    f"{check}_{status}"
    for check in ("oracle", "pristine_checkers")
    for status in ("timeout", "not_run")
)

STRATA = ("memory_dependent", "memory_independent", "all")
COUNTS = (
    "agent_launches",
    "review_rounds",
    "review_fix_cycles",
    "repeated_failed_approaches",
    "human_blocked_interventions",
    "timeouts",
    "truncations",
    "lost_success",
    "false_oracle",
    "checker_altered",
    "stale_completed_nodes",
    "hidden_leaks",
    "journal_bad_lines",
)
# The counts a run log's lines decide: with a line lost, each may be short.
JOURNAL_COUNTS = (
    "agent_launches",
    "review_fix_cycles",
    "repeated_failed_approaches",
    "human_blocked_interventions",
    "timeouts",
    "truncations",
    "lost_success",
)
# The per-run values whose provenance can be anything but what the harness saw.
MEASURES = ("cost_usd", "context_tokens", "duplicate_exploration")
# A rate or count where more is better; all others are worse when higher.
HIGHER_IS_BETTER = ("verified_success",)
DECISION_METRICS = (
    "verified_success",
    *COUNTS,
    *MEASURES,
    "wall_clock_seconds",
    "cost_per_verified_success",
)
HARD_GATES = ("safety_failures", "stale_memory_leaks", "memory_overhead", "crash_recovery_failures")
# A record's Memory overhead, and the pre-registered ceiling each is held to.
MEMORY_OVERHEAD = {
    "storage_bytes": "storage_bytes_per_issue",
    "projection_chars": "projection_chars_per_launch",
    "latency_ms": "latency_ms_per_launch",
}
MEMORY_MEASURES = ("stale_leaks", *MEMORY_OVERHEAD)

GO, NO_GO, INCONCLUSIVE, NOT_EVALUATED = "GO", "NO_GO", "INCONCLUSIVE", "NOT_EVALUATED"
SETTINGS_STATUSES = ("established", "unknown", "mismatch")
# What a report reads of a record beyond its counts and usage: said, or refused.
RECORD_KEYS = (
    "unverified_reasons",
    "run_evidence",
    "prior_experience",
    "settings_digest",
    "settings_evidence",
    "certificate",
    "pristine_checkers",
    "altered_checkers",
    "unknown_counts",
    "safety_failures",
    "safety_assessed",
    "stopped_reason",
    "journal",
    "journal_problems",
    "volatile",
)
# The seed of a smoke run: scored like any run, and outside every plan, so no
# comparison ever pairs it.
SMOKE_SEED = 0

# Where a task's checkers live, here and in every fixture. A candidate may add to it
# and may edit what its reference commit edited (checker_alterations).
CHECKER_ROOT = "tests/"
# What every task protects outright: a change to any of these changes what validation
# decides, whichever test it runs.
PROTECTED_PATHS = (
    "tests/conftest.py",
    "tests/fixtures/",
    "conftest.py",
    "pytest.ini",
    "tox.ini",
    "setup.cfg",
    "pyproject.toml",
    "Makefile",
    ".github/",
)
# The most one lesson may outweigh its match's, so that a control's Memory is as full.
LESSON_LENGTH_RATIO = 2

# The tools whose repeated call is exploring the same thing again.
READ_ONLY_TOOLS = frozenset({"read", "grep", "glob", "find", "ls"})
_PROGRESS_TOOL = re.compile(r"^\[[^\]]*\] (?P<tool>\S+)(?: (?P<detail>.+))?$")
# The line ``StepLog.close`` ends a progress file with when its bound left calls out.
_PROGRESS_OMITTED = re.compile(r"^\[\d+ progress line\(s\) omitted past the \d+-byte bound\]$")
# How a launch's read context is named (``_write_read_context``): phase, attempt, token.
_READ_CONTEXT_DIR = r"[a-z0-9_]+-\d+-[0-9a-f]{12}"
# One file of a read context's index (``ReadContext.rendered``).
_INDEXED_FILE = re.compile(rb"^- `([^`\n]+)`: ", re.M)
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_COUPLED = re.compile(r"\b(ImportError|ModuleNotFoundError|AttributeError|NameError)\b")
_FAILED_MODULE = re.compile(r"^(?:FAILED|ERROR) (tests/[^\s:]+\.py)", re.M)
# What a score reads of a run log line, each as ``ExecutionRecord`` always writes it. A
# line without one, or with another type, could only be read by guessing what it meant.
_LINE_FIELDS: Mapping[str, type | tuple[type, ...]] = {
    "phase": str,
    "profile": str,
    "provider": str,
    "model": str,
    "effort": str,
    "command": list,
    "metadata": dict,
    "provider_summary": dict,
    "sandbox": dict,
    "parsed_result": (dict, type(None)),
    "timed_out": bool,
    "stdout_truncated": bool,
    "stderr_truncated": bool,
    "started_at": str,
    "finished_at": str,
}


class EvaluationError(AutoForgeError):
    """A manifest, record or run this harness refuses to score."""


# -- the pinned files ------------------------------------------------------------------------


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("ascii")).hexdigest()


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def load_manifest() -> dict:
    return read_json(MANIFEST)


def load_preregistration() -> dict:
    return read_json(PREREGISTRATION)


def tasks_by_id(manifest: Mapping) -> dict[str, dict]:
    return {task["id"]: task for task in manifest["tasks"]}


def arms_by_id(manifest: Mapping) -> dict[str, dict]:
    return {arm["id"]: arm for arm in manifest["arms"]}


def hidden_names(task: Mapping) -> list[str]:
    """Each hidden test's name."""
    return [node.partition("::")[2] for node in task["hidden_tests"]]


def forbidden_markers(task: Mapping) -> list[str]:
    """What must never reach an agent: each hidden test's name and each pinned commit.

    A commit by its first seven characters, which every longer spelling of it holds. The
    base counts because the reference is its child, on a history anyone can read.
    """
    return [*hidden_names(task), task["reference_sha"][:7], task["base_sha"][:7]]


def statement_leaks(task: Mapping) -> list[str]:
    """What of the answer a task's statement gives away, which the agent would read."""
    return [marker for marker in forbidden_markers(task) if marker in task["statement"]]


def manifest_problems(manifest: Mapping) -> list[str]:
    """Why the manifest cannot be what a real run is pinned to; empty when it can."""
    problems = []
    if manifest.get("schema") != MANIFEST_SCHEMA:
        problems.append(f"schema is not {MANIFEST_SCHEMA}")
    arm_ids = [arm["id"] for arm in manifest["arms"]]
    if len(set(arm_ids)) != len(arm_ids):
        problems.append("an arm id repeats")
    for arm in manifest["arms"]:
        if arm["status"] == "available" and arm["requires"]:
            problems.append(f"{arm['id']} is available and still requires {arm['requires']}")
        if arm["status"] == "not_evaluated" and not arm["requires"]:
            problems.append(f"{arm['id']} is not evaluated and names no missing feature")
        if arm["status"] not in ("available", "not_evaluated"):
            problems.append(f"{arm['id']} has status {arm['status']!r}")
        if arm.get("seeded_experience") is not (333 in arm["requires"]):
            problems.append(f"{arm['id']}: only an arm with Issue Memory starts with a lesson")
    ids = [task["id"] for task in manifest["tasks"]]
    if len(set(ids)) != len(ids):
        problems.append("a task id repeats")
    tasks = tasks_by_id(manifest)
    for task in manifest["tasks"]:
        name = task["id"]
        for key in ("base_sha", "reference_sha", "base_tree"):
            if not _FULL_SHA.fullmatch(task[key]):
                problems.append(f"{name}: {key} is no full SHA")
        if task["memory_class"] not in STRATA[:2]:
            problems.append(f"{name}: memory_class {task['memory_class']!r}")
        for node in task["hidden_tests"]:
            path = node.partition("::")[0]
            if path not in task["hidden_files"]:
                problems.append(f"{name}: {node} is not in a hidden file")
        if not task["hidden_tests"]:
            problems.append(f"{name}: no hidden test")
        leaks = statement_leaks(task)
        if leaks:
            problems.append(f"{name}: the statement gives away {leaks}")
        other = tasks.get(task["control_for"])
        if other is None or other["control_for"] != name:
            problems.append(f"{name}: control_for is not mutual")
        elif other["memory_class"] == task["memory_class"]:
            problems.append(f"{name}: its match is of the same memory class")
        else:
            problems += _lesson_problems(task, other)
        missing = [path for path in PROTECTED_PATHS if path not in task["protected_paths"]]
        if missing:
            problems.append(f"{name}: protected_paths leaves out {missing}")
    return problems


def _lesson_problems(task: Mapping, match: Mapping) -> list[str]:
    """Why a task's prior experience could not seed a Memory arm; empty when it could."""
    name, prior = task["id"], task["prior_experience"]
    problems = []
    if not _FULL_SHA.fullmatch(prior["evidence_sha"]):
        problems.append(f"{name}: evidence_sha is no full SHA")
    if prior["evidence_sha"] == task["reference_sha"]:
        problems.append(f"{name}: the lesson's evidence is the reference commit")
    if not prior["evidence_test"].partition("::")[2]:
        problems.append(f"{name}: evidence_test names no test")
    lesson, other = prior["lesson"], match["prior_experience"]["lesson"]
    if not lesson.strip():
        problems.append(f"{name}: the lesson is empty")
    leaks = [marker for marker in forbidden_markers(task) if marker in lesson]
    if leaks:
        problems.append(f"{name}: the lesson gives away {leaks}")
    if len(lesson) > LESSON_LENGTH_RATIO * len(other):
        problems.append(f"{name}: its lesson is more than twice its match's length")
    return problems


def prior_experience_digest(task: Mapping) -> str:
    """What a seeded arm's Memory must hold when a run of ``task`` starts."""
    return digest(task["prior_experience"])


def seeded_digest(manifest: Mapping, arm: str, task: Mapping) -> str:
    """The digest a record of ``arm`` on ``task`` must carry; ``""`` for an unseeded arm."""
    return prior_experience_digest(task) if arms_by_id(manifest)[arm]["seeded_experience"] else ""


def preregistration_problems(prereg: Mapping, manifest: Mapping) -> list[str]:
    """Why the pre-registration cannot decide over this manifest; empty when it can."""
    problems = []
    if prereg.get("schema") != PREREGISTRATION_SCHEMA:
        problems.append(f"schema is not {PREREGISTRATION_SCHEMA}")
    arms, sample = arms_by_id(manifest), prereg["sample"]
    classes = Counter(task["memory_class"] for task in manifest["tasks"])
    if sample["tasks"] != len(manifest["tasks"]):
        problems.append("the sample's task count is not the manifest's")
    for stratum in STRATA[:2]:
        if sample[stratum] != classes[stratum]:
            problems.append(f"the sample's {stratum} count is not the manifest's")
    for name, comparison in prereg["comparisons"].items():
        if comparison["primary"]["metric"] not in DECISION_METRICS:
            problems.append(f"{name}: unknown primary metric")
        for contrast in comparison["contrasts"]:
            for side in ("baseline", "treatment"):
                if contrast[side] not in arms:
                    problems.append(f"{name}: unknown arm {contrast[side]!r}")
                if contrast[side] not in prereg["ceilings"]["per_run_usd"]:
                    problems.append(f"{name}: {contrast[side]} has no cost ceiling")
        for criterion in comparison["non_regression"]:
            if criterion["metric"] not in DECISION_METRICS:
                problems.append(f"{name}: unknown metric {criterion['metric']!r}")
            if criterion["stratum"] not in STRATA:
                problems.append(f"{name}: unknown stratum {criterion['stratum']!r}")
            if ("margin" in criterion) == ("relative_margin" in criterion):
                problems.append(f"{name}: {criterion['metric']} needs one margin")
        for gate in comparison["hard_gates"]:
            if gate not in HARD_GATES:
                problems.append(f"{name}: unknown hard gate {gate!r}")
    return problems


def comparison_arms(comparison: Mapping) -> list[str]:
    """Each arm of a comparison once, in the order its contrasts name them."""
    return list(
        dict.fromkeys(
            arm
            for contrast in comparison["contrasts"]
            for arm in (contrast["baseline"], contrast["treatment"])
        )
    )


def cost_ceiling_usd(prereg: Mapping, name: str) -> int:
    """A comparison's ceiling: each planned run at its arm's per-run ceiling."""
    sample = prereg["sample"]
    pairs = sample["tasks"] * len(sample["seeds"])
    ceilings = prereg["ceilings"]["per_run_usd"]
    return sum(pairs * ceilings[arm] for arm in comparison_arms(prereg["comparisons"][name]))


# -- the settings every arm holds equal ------------------------------------------------------

# Every section of AutoForgeConfig belongs to the digest unless named here, so a
# new key under `validation:` (ADR 0010's `validation.fast_commands`, say) is
# held equal the day it lands. A new top-level section is refused until it is
# classified: a Memory arm's own section is that arm's treatment, and saying so
# is a decision this table should record.
EQUALIZED_SECTIONS = (
    "version",
    "prompt_version",
    "execution",
    "safety",
    "github",
    "merge",
    "review",
    "workflow",
    "sandbox",
    "validation",
    "scheduler",
    "profiles",
)
# Where this host and this run keep things, and how the value was set: equal
# across a pair by the protocol's one-host rule, and no part of a real run's
# behaviour the arms could differ in.
EXEMPT_SETTINGS = (
    "state_dir",
    "execution.worktree_dir",
    "github.command",
    "sandbox.read_only_paths",
    "safety.allow_merge_source",
)


def equalized_settings(cfg: AutoForgeConfig) -> dict:
    """What of ``cfg`` the arms of a pair must hold equal; an unclassified section raises."""
    data = dataclasses.asdict(cfg)
    known = {*EQUALIZED_SECTIONS, *(path.partition(".")[0] for path in EXEMPT_SETTINGS)}
    unknown = sorted(set(data) - known)
    if unknown:
        raise EvaluationError(
            f"configuration section(s) {unknown} are neither held equal nor exempt: "
            "classify them in tests/evaluation.py"
        )
    for path in EXEMPT_SETTINGS:
        section, _, key = path.partition(".")
        if key:
            del data[section][key]
        else:
            del data[section]
    return data


def settings_digest(cfg: AutoForgeConfig) -> str:
    return digest(equalized_settings(cfg))


def _setting_paths(settings: Mapping, prefix: str = "") -> list[str]:
    """Each setting's dotted path; an empty mapping, such as a profile's options, is one."""
    paths: list[str] = []
    for key, value in settings.items():
        if isinstance(value, Mapping) and value:
            paths += _setting_paths(value, f"{prefix}{key}.")
        else:
            paths.append(f"{prefix}{key}")
    return paths


def unrecorded_settings(cfg: AutoForgeConfig) -> list[str]:
    """Each equalized setting no file of a run records, by its dotted path.

    A run's state holds its routing and resolved limits (``config_fingerprint``, the
    one way ``execution.default_*`` reach a launch), its sandbox mode (``sandbox_pin``)
    and its prompt version. Whatever else the configuration says, a new key included,
    is here until something records it.
    """
    routing = ("name", *(field.name for field in dataclasses.fields(ProfileRouting)))
    recorded = {
        "prompt_version",
        "sandbox.mode",
        "execution.default_idle_timeout_seconds",
        "execution.default_max_runtime_seconds",
        *(f"profiles.{name}.{key}" for name in FINGERPRINT_PROFILES for key in routing),
    }
    return sorted(set(_setting_paths(equalized_settings(cfg))) - recorded)


# -- reading a run ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RunInput:
    """One finished run's inputs: where it lies and what scores it."""

    evidence: str
    task: Mapping
    arm: str
    seed: int
    state_dir: Path
    repo: str
    base_commit: str
    live_head: str
    cfg: AutoForgeConfig
    # A commit's oracle status: pass, fail, timeout or not_run.
    oracle: Callable[[str], str]
    manifest_digest: str
    preregistration_digest: str
    safety_failures: tuple[str, ...] = ()
    # Where the operator's check of this run's safety is recorded; "" when nobody says.
    safety_evidence: str = ""
    # The final candidate under the pristine checkers (run_oracle's ``pristine``).
    pristine_checkers: Callable[[str], OracleRun] | None = None
    # What an arm's Memory collector read of the lesson the run started with.
    seeded_experience: str = ""
    # Who stopped the run, when something other than its own verdict ended it.
    stopped_by: str = ""
    stopped_reason: str = ""
    # The configuration file the run must have been recorded from; None: not compared.
    config_file: str | None = None


def _git_read(repo: str, *args: str) -> str | None:
    """One hardened git read of ``repo``; ``None`` when git refuses it, or cuts it short."""
    res = execute(local_git_request(["-C", repo, *args], timeout_seconds=120))
    if res.exit_code != 0 or res.timed_out or res.stdout_truncated:
        return None
    return res.stdout or ""


def _git(repo: str, *args: str) -> str:
    """One hardened git read of ``repo``; ``""`` when git refuses it."""
    return (_git_read(repo, *args) or "").strip()


def _blob(repo: str, spec: str) -> bytes | None:
    """A blob's exact bytes, or ``None``: never a capture cut at its bound or re-encoded.

    The executor keeps a stream's head and tail and replaces what is not UTF-8,
    which is right for a log and wrong for a file laid over an export, so the
    size git reports must be the size read back.
    """
    size = _git(repo, "cat-file", "-s", spec)
    if not size:
        return None
    request = local_git_request(["-C", repo, "cat-file", "blob", spec], timeout_seconds=120)
    request.max_output_bytes = int(size) + 1024
    res = execute(request)
    data = (res.stdout or "").encode("utf-8")
    if res.exit_code != 0 or res.timed_out or res.stdout_truncated or len(data) != int(size):
        return None
    return data


def read_journal(state_dir: Path, run_id: str) -> tuple[list[dict], int, str]:
    """The run log's lines, how many did not parse, and how the read went."""
    root = _open_dir(state_dir)
    try:
        status, data = _journal_bytes(root, f"logs/{run_id}/events.jsonl")
    finally:
        root.close()
    lines: list[dict] = []
    bad = 0
    for raw in data.splitlines():
        try:
            line = json.loads(raw)
        except (ValueError, RecursionError):
            bad += 1
            continue
        if isinstance(line, dict):
            lines.append(line)
        else:
            bad += 1
    return lines, bad, status


def _entries(directory: Path) -> list[str]:
    """Every name directly in ``directory``, whatever each is; none when it cannot be listed."""
    try:
        return sorted(os.listdir(directory))
    except OSError:
        return []


def _subdirectories(directory: Path) -> list[str]:
    try:
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def _step_directories(logs: Path) -> list[str]:
    """The run log's step directories: only an entry of the shape the controller publishes."""
    return [name for name in _subdirectories(logs) if STEP_DIR_RE.match(name)]


def _step_entries(logs: Path) -> list[str]:
    """Every entry named like a step, whatever it is, so that what stands where a step's
    directory belongs (a link, a file) is seen and not passed over."""
    return [name for name in _entries(logs) if STEP_DIR_RE.match(name)]


def _step_number(name: str) -> int:
    return int(name.partition("-")[0])


def _line_fault(line: dict, run_id: str, published: set[int], last_seq: int) -> str:
    """Why a score may not read this run log line, as the problem says it; ``""`` if it may."""
    if line.get("run_id") != run_id:
        return "belong to another run"
    seq = line.get("seq")
    if not isinstance(seq, int) or isinstance(seq, bool) or seq <= last_seq or seq not in published:
        return "have no step number of their own"
    for key, kind in _LINE_FIELDS.items():
        if key not in line or not isinstance(line[key], kind):
            return f"hold a malformed {key}"
    head = (line["parsed_result"] or {}).get("head_sha")
    if head not in (None, "") and not (isinstance(head, str) and _FULL_SHA.fullmatch(head.lower())):
        return "hold a malformed parsed_result.head_sha"
    operator = operator_kind(line)
    if operator is None or (
        not operator and not bookkeeping_kind(line) and launch_kind(line) == "unknown"
    ):
        return "name no known producer"
    return ""


def admitted_lines(
    state_dir: Path, run_id: str, lines: Sequence[dict]
) -> tuple[list[dict], Counter[str]]:
    """The lines a score may read, and why the rest may not, counted.

    A line is this run's, under a step number the logger could have given it: an
    integer above the last, naming a directory it published first (``runlog.py``). It
    holds each field a score reads with the type the controller writes, and names a
    known producer. Nothing is read of any other line, and :func:`run_log_problems`
    reports it, so what it would have decided is ``unknown``.
    """
    published = {_step_number(name) for name in _step_directories(state_dir / "logs" / run_id)}
    admitted: list[dict] = []
    refused: Counter[str] = Counter()
    for line in lines:
        last_seq = admitted[-1]["seq"] if admitted else 0
        fault = _line_fault(line, run_id, published, last_seq)
        if fault:
            refused[fault] += 1
        else:
            admitted.append(line)
    return admitted, refused


def run_log_problems(
    state_dir: Path, run_id: str, lines: Sequence[dict], bad: int, status: str
) -> list[str]:
    """Why the run log may not show every launch of the run; empty when it does.

    A step directory is published before its line is appended (``runlog.py``), so a
    directory no admitted line numbers is a launch the journal lost: a crash inside it,
    or a line torn, deleted or rewritten since. What the journal shows then undercounts,
    and a cost summed from it is a part, however well the other lines read.
    """
    problems = [] if status == "read" else [f"the run log is {status}"]
    if bad:
        problems.append(f"{bad} line(s) of the run log do not parse")
    admitted, refused = admitted_lines(state_dir, run_id, lines)
    problems += [f"{count} line(s) of the run log {why}" for why, count in sorted(refused.items())]
    seqs = {line["seq"] for line in admitted}
    lost = [
        name
        for name in _step_entries(state_dir / "logs" / run_id)
        if _step_number(name) not in seqs
    ]
    if lost:
        problems.append(f"{len(lost)} step(s) have no line in the run log, {lost[0]} first")
    return problems


def _bounded_files(state_dir: Path, relpaths: Sequence[str]) -> tuple[list[bytes], int]:
    """Each file's bytes, read as a run log is, and how many could not be: a link, an
    oversized file or one that went away is left out and counted, never read as empty."""
    root: SafeRoot = _open_dir(state_dir)
    try:
        read = [_journal_bytes(root, relpath) for relpath in relpaths]
    finally:
        root.close()
    return [data for status, data in read if status == "read"], sum(
        status != "read" for status, _ in read
    )


def _readable(root: SafeRoot, relpath: str) -> bytes | None:
    """A file's bytes, read as a run log is; ``None`` for a link, a gap or an oversized file."""
    status, data = _journal_bytes(root, relpath)
    return data if status == "read" else None


def _read_context(root: SafeRoot, state_dir: Path, directory: str) -> tuple[list[bytes], int]:
    """One launch's read context, and how many of its inputs could not be read.

    Its index says what the controller wrote, so a file it lists is expected whether or
    not it is still there, and anything else in the directory, of any kind, is an input
    too. An index that is gone, or holds no listing, leaves nothing to say what was shown.
    """
    index = _readable(root, f"{directory}/{INDEX_FILE}")
    listed = {name.decode("utf-8", "replace") for name in _INDEXED_FILE.findall(index or b"")}
    shown = [] if index is None else [index]
    lost = int(index is None or b"\nFiles:\n" not in index)
    for name in sorted((listed | set(_entries(state_dir / directory))) - {INDEX_FILE}):
        data = _readable(root, f"{directory}/{name}")
        if data is None:
            lost += 1
        else:
            shown.append(data)
    return shown, lost


def _shown_to_agents(
    state_dir: Path, run_id: str, lines: Sequence[dict]
) -> tuple[list[bytes], int]:
    """What the run's agents were shown, and how many of those inputs could not be read.

    What is expected comes from what the run recorded, not from what survived it, so an
    input that is gone is counted rather than passed over. Every step holds a prompt. Every
    prompt but one an admitted line (``lines``) says was no agent's names the read context
    written for its launch, which a directory of that run must hold (:func:`_read_context`).
    A directory no prompt names is read too: a launch that crashed left it behind.
    """
    others = {line["seq"] for line in lines} - {line["seq"] for line in _agent_lines(lines)}
    context = f"read-context/{run_id}"
    named = re.compile(rf"{re.escape(context)}/({_READ_CONTEXT_DIR})".encode())
    launches = set(_entries(state_dir / context))
    shown: list[bytes] = []
    lost = 0
    root = _open_dir(state_dir)
    try:
        for step in _step_entries(state_dir / "logs" / run_id):
            prompt = _readable(root, f"logs/{run_id}/{step}/prompt.md")
            if prompt is None:
                lost += 1
                continue
            shown.append(prompt)
            contexts = {match.decode() for match in named.findall(prompt)}
            lost += not contexts and _step_number(step) not in others
            launches |= contexts
        for launch in sorted(launches):
            files, unread = _read_context(root, state_dir, f"{context}/{launch}")
            shown += files
            lost += unread
    finally:
        root.close()
    return shown, lost


def count_leaks(
    state_dir: Path, run_id: str, markers: Sequence[str], *, lines: Sequence[dict] = ()
) -> tuple[int, int]:
    """How many inputs shown to an agent name something it must not know, and how many
    could not be scanned: a leak there is not ruled out. A commit's SHA is the same commit
    in capitals, so case does not hide a marker."""
    needles = [marker.lower().encode() for marker in markers]
    shown, lost = _shown_to_agents(state_dir, run_id, lines)
    found = sum(any(needle in data.lower() for needle in needles) for data in shown)
    return found, lost


def duplicate_exploration(
    state_dir: Path,
    run_id: str,
    provenance: str,
    *,
    lines: Sequence[dict] = (),
    log_complete: bool = True,
) -> dict:
    """Read-only tool calls that repeat one the run already made, across its launches.

    From the allow-listed detail each progress line shows, so a `Bash` `cat` is
    not seen. A run whose log shows no read-only call at all is ``unknown``, not
    zero: a provider whose progress names no tool shows none either. So is one
    whose log lost a launch or an expected progress file: the controller publishes
    one before each launch (``RunLogger.begin_step``), so one that is missing, a link
    or unreadable holds calls nobody can count, and a total without them is a part.
    So is one cut at its bound, which says so in its last line (``StepLog.close``):
    any call it left out could be a repeat. Every step is expected to hold one unless
    an admitted line (``lines``) says it was no agent's, and an agent's line must have
    its step.
    """
    logs = state_dir / "logs" / run_id
    agents = {line["seq"] for line in _agent_lines(lines)}
    others = {line["seq"] for line in lines} - agents
    steps = _step_entries(logs)
    relpaths = [
        f"logs/{run_id}/{step}/progress.log" for step in steps if _step_number(step) not in others
    ]
    seen: Counter[tuple[str, str]] = Counter()
    files, unread = _bounded_files(state_dir, relpaths)
    if unread or not log_complete or agents - {_step_number(step) for step in steps}:
        return {"value": None, "provenance": UNKNOWN}
    for data in files:
        for line in data.decode("utf-8", "replace").splitlines():
            if _PROGRESS_OMITTED.match(line):
                return {"value": None, "provenance": UNKNOWN}
            match = _PROGRESS_TOOL.match(line)
            if match and match.group("tool").lower() in READ_ONLY_TOOLS:
                seen[(match.group("tool").lower(), match.group("detail") or "")] += 1
    if not seen:
        return {"value": None, "provenance": UNKNOWN}
    return {"value": sum(seen.values()) - len(seen), "provenance": provenance}


def reported_candidates(lines: Sequence[Mapping]) -> list[str]:
    """Every commit an agent's accepted result named, in order, repeats kept."""
    found = []
    for line in lines:
        result = line.get("parsed_result")
        if not isinstance(result, dict) or launch_kind(line) != "agent":
            continue
        head = result.get("head_sha")
        if (
            result.get("phase") in ("ANALYZE_EXECUTE", "FIX", "REPLAN_REEXECUTE")
            and isinstance(head, str)
            and _FULL_SHA.fullmatch(head.lower())
        ):
            found.append(head.lower())
    return found


def configured_validation(cfg: AutoForgeConfig) -> Configured:
    """The set and policy a controller's gates hold a run of ``cfg`` to (ADR 0006 D8.1).

    What ``ControllerEngine._validation_policy`` computes from the configuration
    alone, so that this harness holds no engine; a test keeps the two one.
    """
    template: Mapping[str, object] = NO_SANDBOX_TEMPLATE
    if cfg.sandbox.enabled:
        position = NetworkPosition(cfg.sandbox.validation_network)
        paths = tuple(cfg.sandbox.read_only_paths)
        template = launch_template(
            template_facts(LaunchClass.EXPORT_COMMAND, position, operator_paths=paths)
        )
    return Configured(
        definitions(cfg.validation.commands),
        Policy(
            mode=cfg.sandbox.mode,
            template=template,
            env_names=cfg.execution.environment_names(),
            timeout_seconds=cfg.execution.command_timeout_seconds,
            contain_orphans=True,
            method=MATERIALIZATION_METHOD,
        ),
    )


def validation_status(state: AutoForgeState, cfg: AutoForgeConfig, commit: str) -> str:
    """``not_configured``, ``absent``, ``failed`` or ``passed``, from ValidationRecords alone.

    FULL evidence only (ADR 0010 D1, D2): the configured set's latest record for
    this exact commit that applies under the configured policy, enforced where it
    requires that (D8.3). A record of the same commit under another policy says
    nothing of this configuration, and nothing else a run leaves behind can make
    it ``passed``.
    """
    configured = configured_validation(cfg)
    defs = configured.definitions
    if not defs:
        return "not_configured"
    outcomes: dict[str, Outcome] = {}
    for view in state.validation_evidence():
        if view.candidate.commit != commit:
            continue
        for definition in defs:
            if applicable(view, view.candidate, definition.digest, configured.policy):
                outcomes[definition.digest] = view.outcome
    held = [outcomes.get(d.digest) for d in defs]
    if any(outcome is None for outcome in held):
        return "failed" if any(o not in (None, Outcome.PASS) for o in held) else "absent"
    return "passed" if all(outcome is Outcome.PASS for outcome in held) else "failed"


def _instant(stamp: str) -> datetime | None:
    """A stamp as the controller writes one, with its offset; ``None`` for anything else,
    such as the empty one of a launch refused before it could start."""
    try:
        parsed = datetime.fromisoformat(stamp)
    except ValueError:
        return None
    return parsed if parsed.utcoffset() is not None else None


def _wall_clock(
    state: AutoForgeState, lines: Sequence[Mapping], log_complete: bool, provenance: str
) -> dict:
    """From the state file's creation to the last thing the run is on record as doing.

    The engine saves before a launch, and not after one that timed out or failed, so a
    run's last write can lie hours before its end: only that launch's line holds it. A
    launch's stamps must be readable and in order, within the run. With one that is not,
    or a launch lost from the run log, no end is established: the total is ``unknown``, with
    what is known of it as a part, apart. An operator's line is no launch and ends nothing.
    """
    unknown: dict = {"value": None, "provenance": UNKNOWN}
    start, saved = _instant(state.created_at), _instant(state.updated_at)
    if start is None:
        return unknown
    written = saved is not None and saved >= start
    established = log_complete and written
    ends = [saved] if written else []
    for line in lines:
        if operator_kind(line) or launch_kind(line) == "unknown":
            continue
        began, ended = _instant(line["started_at"]), _instant(line["finished_at"])
        if began is None or ended is None or not start <= began <= ended:
            established = False
        else:
            ends.append(ended)
    if established:
        return {"value": (max(ends) - start).total_seconds(), "provenance": provenance}
    if ends:
        unknown["known_part"] = (max(ends) - start).total_seconds()
    return unknown


def settings_evidence(
    state: AutoForgeState,
    agent_lines: Sequence[Mapping],
    cfg: AutoForgeConfig,
    log_complete: bool,
    *,
    config_file: str | None = None,
) -> dict:
    """What the run's own records say of the settings it is scored under.

    ``mismatch``: a record contradicts ``cfg``, so the run was made under other
    settings. ``established``: every equalized setting is held by a record as the run
    was created and is ``cfg``'s, and so is every launch the run log shows.
    ``unknown``: anything else, so nothing says it was: a record is missing or was
    written after the run began, a launch is lost, or the setting is one no run records
    (``unrecorded``, :func:`unrecorded_settings`). A validation record or a certificate
    speaks for its own candidate, never for the configuration as a whole.

    The recorded ``config_file`` is a pathname. Another one contradicts ``config_file``
    here, and the same one says nothing of what that file held when the run began, so
    what ``cfg`` loaded from it today establishes nothing.
    """
    mismatches: list[str] = []
    unknown: list[str] = []
    if state.prompt_version != cfg.prompt_version:
        mismatches.append(f"the run recorded prompt version {state.prompt_version}")
    if not state.config_fingerprint:
        unknown.append("the run recorded no configuration")
    else:
        recorded = RecordedConfiguration.from_dict(state.config_fingerprint)
        if recorded.source == SOURCE_UPGRADED:
            unknown.append("the run's configuration was recorded after it began")
        elif recorded.source != SOURCE_CREATED:
            mismatches.append(f"the run's configuration was {recorded.source} during the run")
        if config_file is not None and recorded.config_file != config_file:
            mismatches.append(
                f"the run was recorded from {recorded.config_file or 'the built-in defaults'}, "
                f"not {config_file}"
            )
        routing = fingerprint_routing(cfg)
        for name in FINGERPRINT_PROFILES:
            if recorded.fingerprint.profiles.get(name) != routing.get(name):
                mismatches.append(f"profile {name} was recorded with other routing or limits")
    if not state.sandbox_pin:
        unknown.append("the run recorded no sandbox pin")
    else:
        pin = SandboxPin.from_dict(state.sandbox_pin)
        if pin.mode != cfg.sandbox.mode:
            mismatches.append(f"the run is pinned to sandbox.mode {pin.mode}")
        if pin.source == SOURCE_UPGRADED:
            unknown.append("the run's sandbox pin was recorded after it began")
        elif pin.source != SOURCE_CREATED:
            mismatches.append(f"the run's sandbox pin was {pin.source} during the run")
    for line in agent_lines:
        profile = cfg.profiles.get(line.get("profile", ""))
        launched = tuple(line.get(key) for key in ("provider", "model", "effort"))
        if profile is None or launched != (profile.provider, profile.model, profile.effort):
            mismatches.append(f"launch {line.get('seq')} ran under other routing")
        if cfg.sandbox.enabled and (line.get("sandbox") or {}).get("enforced") is not True:
            mismatches.append(f"launch {line.get('seq')} was not enforced")
    if not log_complete:
        unknown.append("the run log is incomplete, so not every launch could be compared")
    unrecorded = unrecorded_settings(cfg)
    if unrecorded:
        sections = sorted({path.partition(".")[0] for path in unrecorded})
        unknown.append(
            f"no record of the run holds {len(unrecorded)} of the settings held equal, "
            f"under {', '.join(sections)}"
        )
    status = "mismatch" if mismatches else "unknown" if unknown else "established"
    return {"status": status, "problems": [*mismatches, *unknown], "unrecorded": unrecorded}


@dataclass(frozen=True)
class FileChange:
    """One path a candidate changed from its base; a binary change counts no lines."""

    path: str
    removed: int | None
    new: bool


def candidate_changes(repo: str, base: str, head: str) -> list[FileChange] | None:
    """What ``head`` changed from ``base``, renames as a delete and an add; ``None``
    when git could not say, which is no statement that nothing changed."""
    numstat = _git_read(repo, "diff", "--numstat", "--no-renames", "-z", base, head)
    added = _git_read(
        repo, "diff", "--name-only", "--no-renames", "--diff-filter=A", "-z", base, head
    )
    if numstat is None or added is None:
        return None
    new = set(added.split("\0"))
    changes = []
    for entry in filter(None, numstat.split("\0")):
        _, removed, path = entry.split("\t", 2)
        changes.append(FileChange(path, int(removed) if removed.isdigit() else None, path in new))
    return changes


def touches_protected(changed: Sequence[str], protected: Sequence[str]) -> bool:
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry))
        for path in changed
        for entry in protected
    )


def checker_alterations(changes: Sequence[FileChange], task: Mapping) -> list[str]:
    """The checker inputs a candidate altered, which validation then decided by.

    A protected path, changed in any way. Under :data:`CHECKER_ROOT`, documentation
    aside: a new ``conftest.py``, which reaches its directory's tests; any other
    existing file changed, apart from a task's hidden file, which its reference
    edited too so a correct candidate may have to, and apart from lines only added
    to a ``test_*.py`` module. Those two are held by the pristine suite instead
    (:func:`run_oracle`'s ``pristine``), since a test can also be weakened by
    adding a line. A new test module changes no input that decided anything.
    """
    altered = []
    for change in changes:
        path = change.path
        name = path.rpartition("/")[2]
        if touches_protected([path], task["protected_paths"]):
            altered.append(path)
        elif not path.startswith(CHECKER_ROOT) or path.endswith(".md"):
            continue
        elif change.new:
            if name == "conftest.py":
                altered.append(path)
        elif path in task["hidden_files"]:
            continue
        elif not (name.startswith("test_") and name.endswith(".py") and change.removed == 0):
            altered.append(path)
    return sorted(altered)


def _usage(
    agent_lines: Sequence[Mapping], key: str, provenance: str, scale: float, log_complete: bool
) -> dict:
    """A launch-reported total, or ``unknown`` with what is known of it as a part, apart:
    a launch that reported none, or a run log that lost one, leaves no total to state."""
    summaries = [line.get("provider_summary") or {} for line in agent_lines]
    values = [s.get(key) for s in summaries]
    known = [v for v in values if isinstance(v, int) and not isinstance(v, bool) and v >= 0]
    if log_complete and agent_lines and len(known) == len(values):
        return {"value": sum(known) * scale, "provenance": provenance}
    unknown: dict = {"value": None, "provenance": UNKNOWN}
    if known:
        unknown["known_part"] = sum(known) * scale
    return unknown


def _agent_lines(lines: Sequence[dict]) -> list[dict]:
    return [line for line in lines if not operator_kind(line) and launch_kind(line) == "agent"]


def _empty_counts() -> dict[str, int]:
    return dict.fromkeys(COUNTS, 0)


def _stop_outcome(state: AutoForgeState, agent_lines: Sequence[Mapping]) -> str:
    """How a run that holds no completed candidate stopped, from typed fields only."""
    last = agent_lines[-1] if agent_lines else {}
    if (last.get("provider_summary") or {}).get("auth_preflight") == "refused":
        return "credentials_unavailable"
    if last.get("timed_out") is True:
        return "timeout"
    if state.phase is Phase.BLOCKED:
        return "blocked"
    if state.phase is Phase.FAILED:
        return "failed"
    return "incomplete"


def oracle_counts(oracle: Mapping[str, str], final: str, certified: str) -> dict[str, int | None]:
    """``lost_success`` and ``false_oracle``; ``None`` when the results do not establish one.

    Only a ``pass`` or a ``fail`` says anything of a commit (``ORACLE_RESULTS``), so each is
    read only as far as the results at hand decide it: a final pass loses nothing whatever
    an earlier candidate's oracle did, and an earlier pass with a final candidate nobody could
    assess may or may not be a loss. A run that ended on no candidate holds nothing that
    passes, which takes no oracle to know.
    """
    ended = oracle[final] if final else "fail"
    earlier = [status for commit, status in oracle.items() if commit != final]
    lost: int | None
    if ended == "pass" or all(status == "fail" for status in earlier):
        lost = 0
    else:
        lost = 1 if ended == "fail" and "pass" in earlier else None
    return {
        "lost_success": lost,
        "false_oracle": {"pass": 0, "fail": 1}.get(oracle[certified]) if certified else 0,
    }


def score_run(run: RunInput) -> dict:
    """The run's record, from its state file, run log, git and the oracle.

    Reads and launches nothing else. The state file is loaded by the
    controller's strict loader, the run log as ``tests/stop_corpus.py`` reads
    it, and only typed fields are read: no error text, no agent prose.
    """
    state = load_state(run.state_dir / "state.json")
    read, bad, journal = read_journal(run.state_dir, state.run_id)
    log_problems = run_log_problems(run.state_dir, state.run_id, read, bad, journal)
    log_complete = not log_problems
    lines, _ = admitted_lines(run.state_dir, state.run_id, read)
    agents = _agent_lines(lines)
    applied = sum(operator_kind(line) == "unblock-applied" for line in lines)
    provenance = MEASURED if run.evidence == REAL else SYNTHETIC

    certificate = state.review_certificate or {}
    certified = parse_candidate(certificate["candidate"]).commit if certificate else ""
    certificate_issue = (
        certificate_problem(certificate, state, configured_validation(run.cfg))
        if certificate
        else ""
    )
    reported = reported_candidates(lines)
    final = run.live_head or state.current_head_sha
    candidates = list(dict.fromkeys(c for c in (*reported, certified, final) if c))
    oracle = {commit: run.oracle(commit) for commit in candidates}
    final_oracle = oracle.get(final, "not_run")
    validation = validation_status(state, run.cfg, final) if final else "absent"

    counts = _empty_counts()
    counts["agent_launches"] = len(agents)
    counts["review_rounds"] = state.review_round
    counts["review_fix_cycles"] = sum(
        line.get("phase") == "FIX" and isinstance(line.get("parsed_result"), dict)
        for line in agents
    )
    counts["human_blocked_interventions"] = applied + int(state.phase is Phase.BLOCKED)
    counts["timeouts"] = sum(line.get("timed_out") is True for line in agents)
    counts["truncations"] = sum(
        line.get("stdout_truncated") is True or line.get("stderr_truncated") is True
        for line in lines
    )
    counts["journal_bad_lines"] = bad + len(read) - len(lines)
    counts["hidden_leaks"], unscanned = count_leaks(
        run.state_dir,
        state.run_id,
        run.task.get("leak_markers") or forbidden_markers(run.task),
        lines=lines,
    )
    established = oracle_counts(oracle, final, certified)
    counts |= {key: value or 0 for key, value in established.items()}
    counts["stale_completed_nodes"] = int(bool(certified) and certified != final)
    unknown_counts = list(JOURNAL_COUNTS) if not log_complete else []
    unknown_counts += [key for key, value in established.items() if value is None]
    if unscanned:
        unknown_counts.append("hidden_leaks")
    changes = candidate_changes(run.repo, run.base_commit, final) if final else []
    if changes is None:
        unknown_counts.append("checker_altered")
        changes = []
    altered = checker_alterations(changes, run.task)
    counts["checker_altered"] = int(bool(altered))
    failed_trees = {
        view.candidate.tree
        for view in state.validation_evidence()
        if view.outcome is not Outcome.PASS
    }
    seen_failed: set[str] = set()
    for commit in reported:
        tree = _git(run.repo, "rev-parse", f"{commit}^{{tree}}")
        if tree in seen_failed:
            counts["repeated_failed_approaches"] += 1
        if tree in failed_trees:
            seen_failed.add(tree)

    reasons: list[str] = []
    pristine = {"status": "not_run", "failing_modules": []}
    if state.phase in (Phase.READY_FOR_MERGE, Phase.MERGE, Phase.DONE):
        if validation != "passed":
            reasons.append(f"validation_{validation}")
        if final_oracle != "pass":
            reasons.append("oracle_failed" if final_oracle == "fail" else f"oracle_{final_oracle}")
        if not certificate:
            reasons.append("no_certificate")
        elif certified != final:
            reasons.append("stale_certificate")
        if certificate_issue:
            reasons.append("certificate_not_applicable")
        if counts["checker_altered"]:
            reasons.append("checker_altered")
        if "checker_altered" in unknown_counts:
            reasons.append("checker_inputs_unread")
        if counts["hidden_leaks"]:
            reasons.append("hidden_test_leak")
        if unscanned:
            reasons.append("leak_scan_incomplete")
        # A whole suite more, so only for a run whose outcome it could still change.
        if not reasons and not run.stopped_by:
            pristine = _pristine_verdict(run, final, changes, counts, reasons)
            if pristine["status"] not in ORACLE_RESULTS:
                unknown_counts.append("checker_altered")
        outcome = UNVERIFIED if reasons else VERIFIED
    else:
        outcome = _stop_outcome(state, agents)
    if run.stopped_by:
        check_stopped(run.stopped_by, run.stopped_reason)
        outcome = run.stopped_by

    safety_failures = list(run.safety_failures)
    if state.counted_merged_prs:
        safety_failures.append("the run merged a pull request")
    return {
        "schema": RUN_SCHEMA,
        "evidence": run.evidence,
        "arm": run.arm,
        "task": run.task["id"],
        "memory_class": run.task["memory_class"],
        "seed": run.seed,
        "manifest_digest": run.manifest_digest,
        "preregistration_digest": run.preregistration_digest,
        "settings_digest": settings_digest(run.cfg),
        "settings_evidence": settings_evidence(
            state, agents, run.cfg, log_complete, config_file=run.config_file
        ),
        "run_evidence": True,
        "prior_experience": run.seeded_experience,
        "phase": state.phase.value,
        "outcome": outcome,
        "unverified_reasons": sorted(reasons),
        "validation": validation,
        "certificate": {
            "held": bool(certificate),
            "candidate": certified,
            "problem": certificate_issue,
        },
        "final_candidate": final,
        "candidates": candidates,
        "oracle": oracle,
        "pristine_checkers": pristine,
        "altered_checkers": altered,
        "counts": counts,
        "unknown_counts": sorted(set(unknown_counts)),
        "usage": {
            "cost_usd": _usage(agents, "cost_micro_usd", provenance, 1e-6, log_complete),
            "context_tokens": _usage(agents, "context_tokens", provenance, 1, log_complete),
            "duplicate_exploration": duplicate_exploration(
                run.state_dir, state.run_id, provenance, lines=lines, log_complete=log_complete
            ),
        },
        # An arm without Memory has neither; its collector reports them when it exists.
        "memory": {
            "stale_leaks": {"value": None, "provenance": NOT_APPLICABLE},
            "storage_bytes": {"value": None, "provenance": NOT_APPLICABLE},
            "projection_chars": {"value": None, "provenance": NOT_APPLICABLE},
            "latency_ms": {"value": None, "provenance": NOT_APPLICABLE},
        },
        "safety_failures": sorted(safety_failures),
        # A clean gate needs someone to have looked, and every launch to be on record.
        "safety_assessed": bool(run.safety_evidence.strip()) and log_complete,
        "safety_evidence": run.safety_evidence,
        "stopped_reason": run.stopped_reason,
        "journal": journal,
        "journal_problems": log_problems,
        # What differs between two runs of the same inputs; no reproducibility
        # check reads it (deterministic_view).
        "volatile": {
            "run_id": state.run_id,
            "wall_clock_seconds": _wall_clock(state, lines, log_complete, provenance),
        },
    }


def _pristine_verdict(
    run: RunInput, final: str, changes: Sequence[FileChange], counts: dict, reasons: list[str]
) -> dict:
    """The final candidate's source under its checkers as pinned, added to ``reasons``.

    The one run that sees a test weakened by an added line or in a hidden file:
    validation passed on the candidate's own copy, and this replaces only that
    copy. So a failure in a module the candidate changed is an altered checker
    that hid it, and one the candidate left alone is a failure of its own. A
    runner that names no module cannot tell them apart, and counts as altered.
    """
    done = run.pristine_checkers(final) if run.pristine_checkers else OracleRun("not_run")
    if done.status == "pass":
        return {"status": "pass", "failing_modules": []}
    if done.status != "fail":
        reasons.append(f"pristine_checkers_{done.status}")
        return {"status": done.status, "failing_modules": []}
    reasons.append("pristine_checkers_failed")
    failing = failing_modules(done.stdout)
    if not failing or {change.path for change in changes} & set(failing):
        counts["checker_altered"] = 1
        reasons.append("checker_altered")
    return {"status": "fail", "failing_modules": failing}


def check_stopped(stopped_by: str, reason: str) -> None:
    if stopped_by not in STOPPED_BY:
        raise EvaluationError(f"--stopped-by must be one of {STOPPED_BY}")
    if not reason.strip():
        raise EvaluationError("a run that did not finish needs a stated reason")


def not_run_record(
    task: Mapping,
    arm: str,
    seed: int,
    stopped_by: str,
    reason: str,
    manifest_digest: str,
    preregistration_digest: str,
) -> dict:
    """A planned run with nothing to read, stopped and said why: never dropped.

    A run that left a state directory goes through :func:`score_run` instead, which
    keeps what it did and spent. Here nothing is known of its settings or its usage.
    """
    check_stopped(stopped_by, reason)
    unknown = {"value": None, "provenance": UNKNOWN}
    return {
        "schema": RUN_SCHEMA,
        "evidence": REAL,
        "arm": arm,
        "task": task["id"],
        "memory_class": task["memory_class"],
        "seed": seed,
        "manifest_digest": manifest_digest,
        "preregistration_digest": preregistration_digest,
        "settings_digest": "",
        "settings_evidence": {"status": "unknown", "problems": ["no run evidence was given"]},
        "run_evidence": False,
        "prior_experience": "",
        "phase": "",
        "outcome": stopped_by,
        "unverified_reasons": [],
        "validation": "absent",
        "certificate": {"held": False, "candidate": "", "problem": ""},
        "final_candidate": "",
        "candidates": [],
        "oracle": {},
        "pristine_checkers": {"status": "not_run", "failing_modules": []},
        "altered_checkers": [],
        "counts": _empty_counts(),
        "unknown_counts": sorted(COUNTS),
        "usage": {key: dict(unknown) for key in MEASURES},
        "memory": {key: {"value": None, "provenance": NOT_APPLICABLE} for key in MEMORY_MEASURES},
        "safety_failures": [],
        "safety_assessed": False,
        "safety_evidence": "",
        "stopped_reason": reason,
        "journal": "absent",
        "journal_problems": ["no run evidence was given"],
        "volatile": {"run_id": "", "wall_clock_seconds": dict(unknown)},
    }


def record_problems(record: Mapping) -> list[str]:
    """Why a record file cannot enter a report; empty when it can."""
    problems = []
    if record.get("schema") != RUN_SCHEMA:
        return [f"schema is not {RUN_SCHEMA}"]
    if record.get("evidence") not in (FIXTURE, REAL):
        problems.append(f"evidence {record.get('evidence')!r}")
    if record.get("outcome") not in OUTCOMES:
        problems.append(f"outcome {record.get('outcome')!r}")
    for key in COUNTS:
        if not isinstance(record.get("counts", {}).get(key), int):
            problems.append(f"counts.{key} is no integer")
    problems += [f"{key} is not stated" for key in RECORD_KEYS if key not in record]
    if not set(record.get("unknown_counts", ())) <= set(COUNTS):
        problems.append("unknown_counts names something that is no count")
    if record.get("settings_evidence", {}).get("status") not in SETTINGS_STATUSES:
        problems.append("settings_evidence has no known status")
    for key in MEASURES:
        measure = record.get("usage", {}).get(key)
        if not isinstance(measure, dict) or measure.get("provenance") not in PROVENANCES:
            problems.append(f"usage.{key} has no known provenance")
    for key in MEMORY_MEASURES:
        measure = record.get("memory", {}).get(key)
        if not isinstance(measure, dict) or measure.get("provenance") not in PROVENANCES:
            problems.append(f"memory.{key} has no known provenance")
    if record.get("evidence") == FIXTURE:
        # A fixture cannot report what only a real provider could have measured.
        for key in MEASURES:
            if record.get("usage", {}).get(key, {}).get("provenance") == MEASURED:
                problems.append(f"usage.{key} of a fixture run says measured")
    return problems


def deterministic_view(record: Mapping) -> dict:
    """A record without what legitimately differs between two runs of the same inputs."""
    return {key: value for key, value in record.items() if key != "volatile"}


# -- the oracle ------------------------------------------------------------------------------


@dataclass(frozen=True)
class OracleRun:
    status: str
    stdout: str = ""


def oracle_command(manifest: Mapping, task: Mapping) -> list[str]:
    return [*manifest["oracle_command_prefix"], *task["hidden_tests"]]


def validation_command(manifest: Mapping, task: Mapping) -> list[str]:
    """The task's FULL set: the suite, without the modules that already fail at its base.

    A module the base itself fails would fail every candidate of every arm, so the
    run could never succeed, whatever the agent did. Whole modules rather than
    single tests: a definition is bounded at 32 argv elements (ADR 0006 D10.3).
    """
    ignored = [f"--ignore={path}" for path in task["validation_ignore"]]
    return [*manifest["validation_command_prefix"], *ignored]


def task_config(manifest: Mapping, task: Mapping) -> AutoForgeConfig:
    """The pinned configuration as a run of ``task`` holds it."""
    cfg = load_config_file(str(REAL_RUN_CONFIG))
    cfg.validation.commands = [validation_command(manifest, task)]
    return cfg


def hidden_overlay(oracle_repo: str, task: Mapping) -> dict[str, bytes]:
    """The hidden files as the reference commit holds them, read from the store outside."""
    overlay = {}
    for path in task["hidden_files"]:
        data = _blob(oracle_repo, f"{task['reference_sha']}:{path}")
        if data is None:
            raise EvaluationError(f"{task['id']}: the oracle store gives no exact {path}")
        overlay[path] = data
    return overlay


def _write_overlay(root: Path, overlay: Mapping[str, bytes]) -> None:
    for relpath, data in overlay.items():
        target = (root / relpath).resolve()
        if not target.is_relative_to(root.resolve()):
            raise EvaluationError(f"a hidden file leaves the export: {relpath}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _restore_checkers(runner, pristine: tuple[str, str], root: Path) -> None:
    """Replace the export's ``tests/`` by a pinned commit's, what the candidate added gone."""
    store, commit = pristine
    with export_commit_tree(runner, store, commit) as source:
        target = root / CHECKER_ROOT
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source.root / CHECKER_ROOT, target, symlinks=True)


def _oracle_sandbox(cfg: AutoForgeConfig, cwd: str, overrides, denied: tuple[str, ...]):
    """The export-command description ``validation.commands`` run in, or None when disabled."""
    if not cfg.sandbox.enabled:
        return None
    facts = LaunchFacts(
        launch_class=LaunchClass.EXPORT_COMMAND,
        workspace=cwd,
        home=operator_home(),
        controller_env=dict(os.environ),
        controller_config=overrides_directory(overrides),
        denied_roots=denied,
        network=NetworkPosition(cfg.sandbox.validation_network),
        operator_paths=tuple(cfg.sandbox.read_only_paths),
    )
    env = {**select_environment(cfg.execution.environment_names()), **overrides}
    return describe_launch(facts, None, env)


def run_oracle(
    repo: str,
    commit: str,
    overlay: Mapping[str, bytes],
    command: Sequence[str],
    cfg: AutoForgeConfig,
    *,
    denied_roots: tuple[str, ...] = (),
    runner=execute,
    pristine: tuple[str, str] | None = None,
) -> OracleRun:
    """Run the hidden tests against ``commit``, as ``validation.commands`` run.

    An exact export with the hidden files laid over it, so what the candidate
    did to those paths does not matter; the allow-listed environment and the
    credential preflight; the export-command sandbox under ``required``. What
    could not be launched or was refused is ``not_run``: only exit 0 is a pass.

    With ``pristine`` (a store and a commit) the whole of ``tests/`` comes from that
    commit instead, so ``commit``'s source meets every checker as it is pinned.
    """
    names = cfg.execution.environment_names()
    try:
        with export_commit_tree(runner, repo, commit) as exported:
            if pristine is not None:
                _restore_checkers(runner, pristine, exported.root)
            _write_overlay(exported.root, overlay)
            cwd = str(exported.root)
            identity = preflight(cwd, None if runner is execute else runner, env_allowlist=names)
            with isolated_overrides(identity) as overrides:
                request = ExecutionRequest(
                    command=list(command),
                    cwd=cwd,
                    timeout_seconds=cfg.execution.command_timeout_seconds,
                    env_allowlist=names,
                    env=overrides,
                    contain_orphans=True,
                )
                request.sandbox = _oracle_sandbox(cfg, cwd, overrides, denied_roots)
                result = runner(request)
    except (AutoForgeError, OSError) as exc:
        return OracleRun("not_run", type(exc).__name__)
    if result.timed_out:
        return OracleRun("timeout", result.stdout or "")
    return OracleRun("pass" if result.exit_code == 0 else "fail", result.stdout or "")


def verify_pin_history(repo: str, task: Mapping) -> list[str]:
    """Why a task's pins do not match this history; empty when they do."""
    problems = []
    reference, base = task["reference_sha"], task["base_sha"]
    if _git(repo, "rev-parse", f"{reference}^") != base:
        problems.append(f"{task['id']}: {base} is not the reference commit's parent")
    if _git(repo, "rev-parse", f"{base}^{{tree}}") != task["base_tree"]:
        problems.append(f"{task['id']}: base_tree is not the base commit's tree")
    for path in task["hidden_files"]:
        if not _git(repo, "rev-parse", f"{reference}:{path}"):
            problems.append(f"{task['id']}: the reference commit holds no {path}")
    for node in task["hidden_tests"]:
        path, _, name = node.partition("::")
        text = (_blob(repo, f"{reference}:{path}") or b"").decode("utf-8", "replace")
        if not re.search(rf"^(?:async )?def {re.escape(name)}\(", text, re.M):
            problems.append(f"{task['id']}: {path} defines no {name} at the reference commit")
    changed = _git(repo, "diff", "--name-only", "--no-renames", base, reference).splitlines()
    outside_hidden = [path for path in changed if path not in task["hidden_files"]]
    if touches_protected(outside_hidden, task["protected_paths"]):
        problems.append(f"{task['id']}: the reference commit changes a protected path")
    return problems + verify_pin_lesson(repo, task)


def verify_pin_lesson(repo: str, task: Mapping) -> list[str]:
    """Why a task's lesson is no earlier verified experience of this history; empty if it is.

    Its evidence is at or before the base, so the run's own attempt cannot have
    taught it, and it names a test at that commit. It copies no source line the
    reference commit added: the mechanical half of "no hidden answer", the
    other half being that it names no hidden test (:func:`manifest_problems`).
    """
    prior, name = task["prior_experience"], task["id"]
    evidence, base = prior["evidence_sha"], task["base_sha"]
    problems = []
    if _git(repo, "merge-base", evidence, base) != evidence:
        problems.append(f"{name}: the lesson's evidence is not at or before the base")
    path, _, test = prior["evidence_test"].partition("::")
    text = (_blob(repo, f"{evidence}:{path}") or b"").decode("utf-8", "replace")
    if not re.search(rf"^(?:async )?def {re.escape(test)}\(", text, re.M):
        problems.append(f"{name}: {path} defines no {test} at the lesson's evidence")
    fix = _git(repo, "diff", "-U0", base, task["reference_sha"], "--", "src/").splitlines()
    added = [
        line[1:].strip() for line in fix if line.startswith("+") and not line.startswith("+++")
    ]
    copied = [line for line in added if len(line) >= 24 and line in prior["lesson"]]
    if copied:
        problems.append(f"{name}: the lesson copies the fix: {copied[0]!r}")
    return problems


def verify_pin_oracle(repo: str, manifest: Mapping, task: Mapping, cfg: AutoForgeConfig) -> dict:
    """A task's oracle must fail at its base, on behaviour, and pass at its reference.

    A failure at the base that names a missing symbol is a hidden test coupled to
    how the reference commit happened to spell the fix, which an independent
    solution could not pass, so the task is no fair task.
    """
    overlay = hidden_overlay(repo, task)
    command = oracle_command(manifest, task)
    at_base = run_oracle(repo, task["base_sha"], overlay, command, cfg)
    at_reference = run_oracle(repo, task["reference_sha"], overlay, command, cfg)
    prior = task["prior_experience"]
    lesson = run_oracle(
        repo,
        prior["evidence_sha"],
        {},
        [*manifest["oracle_command_prefix"], prior["evidence_test"]],
        cfg,
    )
    return {
        "base": at_base.status,
        "reference": at_reference.status,
        "coupled_to_new_names": bool(_COUPLED.search(at_base.stdout)),
        "lesson": lesson.status,
    }


def failing_modules(stdout: str) -> list[str]:
    """The test modules a pytest summary (``-rfE``) names as failed or in error."""
    return sorted(set(_FAILED_MODULE.findall(stdout)))


def verify_base_suite(
    repo: str, manifest: Mapping, task: Mapping, cfg: AutoForgeConfig, workers: int
) -> dict:
    """What the task's validation does at its base, launched as a candidate's would be.

    ``pass`` is what a run needs of its host before an agent has done anything. The
    answer depends on the host, its interpreter, ``sandbox.mode`` and the workers,
    so the run host's own answer is what a real run may rely on.
    """
    return _suite_at(repo, manifest, task, task["base_sha"], cfg, workers)


def verify_reference_suite(
    repo: str, manifest: Mapping, task: Mapping, cfg: AutoForgeConfig, workers: int
) -> dict:
    """What the pristine checkers do at the reference commit, the one source they were
    pinned against. Anything but ``pass`` would refuse every candidate of every arm."""
    return _suite_at(repo, manifest, task, task["reference_sha"], cfg, workers)


def _suite_at(
    repo: str, manifest: Mapping, task: Mapping, commit: str, cfg: AutoForgeConfig, workers: int
) -> dict:
    options = ["--tb=no", "-rfE", *(("-n", str(workers)) if workers else ())]
    command = [*validation_command(manifest, task), *options]
    run = run_oracle(repo, commit, {}, command, cfg)
    return {"status": run.status, "failing_modules": failing_modules(run.stdout)}


def pristine_command(manifest: Mapping, task: Mapping) -> list[str]:
    """The task's validation, saying which modules failed."""
    return [*validation_command(manifest, task), "--tb=no", "-rfE"]


# -- statistics ------------------------------------------------------------------------------


def percentile_interval(
    rows: Sequence, statistic: Callable[[Sequence], float | None], stats: Mapping
) -> tuple[float, float] | None:
    """A task-level bootstrap interval: tasks are resampled, never runs or seeds.

    Runs of one task are not independent evidence, so the interval is as wide as
    the number of tasks makes it however many seeds each has. ``None`` when a
    resample has no value (a relative change from a baseline of nothing).
    """
    rng = random.Random(stats["seed"])
    n = len(rows)
    resamples = stats["resamples"]
    values = []
    for _ in range(resamples):
        value = statistic([rows[rng.randrange(n)] for _ in range(n)])
        if value is None:
            return None
        values.append(value)
    values.sort()
    tail = (1 - stats["confidence"]) / 2
    return values[int(tail * resamples)], values[min(resamples - 1, int((1 - tail) * resamples))]


def mean_difference(rows: Sequence[tuple[float, float]]) -> float:
    return statistics.fmean(treated - base for base, treated in rows)


def relative_change(rows: Sequence[tuple[float, float]]) -> float | None:
    base = statistics.fmean(row[0] for row in rows)
    treated = statistics.fmean(row[1] for row in rows)
    if base == 0:
        return 0.0 if treated == 0 else None
    return treated / base - 1


def measure_of(record: Mapping, metric: str) -> tuple[float | None, str]:
    """A record's value of ``metric`` and where the value came from."""
    observed = MEASURED if record["evidence"] == REAL else SYNTHETIC
    if metric == "verified_success":
        reasons = record["unverified_reasons"]
        if record["outcome"] == UNVERIFIED and reasons and NO_RESULT_REASONS.issuperset(reasons):
            return None, UNKNOWN
        return float(record["outcome"] == VERIFIED), observed
    if metric in record["unknown_counts"]:
        return None, UNKNOWN
    if metric in COUNTS:
        return float(record["counts"][metric]), observed
    if metric == "wall_clock_seconds":
        measure = record["volatile"]["wall_clock_seconds"]
    else:
        measure = record["usage"][metric]
    return measure["value"], measure["provenance"]


# -- decisions -------------------------------------------------------------------------------


def _in_stratum(record: Mapping, stratum: str) -> bool:
    return stratum == "all" or record["memory_class"] == stratum


def _per_task_rows(
    pairs: Sequence[tuple[Mapping, Mapping]], metric: str, stratum: str
) -> tuple[list[tuple[float, float]], Counter[str]]:
    """One (baseline mean, treatment mean) per task, and the values' provenance."""
    by_task: dict[str, list[tuple[float | None, float | None]]] = {}
    provenance: Counter[str] = Counter()
    for base, treated in pairs:
        if not _in_stratum(base, stratum):
            continue
        base_value, base_source = measure_of(base, metric)
        treated_value, treated_source = measure_of(treated, metric)
        provenance.update((base_source, treated_source))
        by_task.setdefault(base["task"], []).append((base_value, treated_value))
    rows = [
        (
            statistics.fmean(pair[0] for pair in values if pair[0] is not None),
            statistics.fmean(pair[1] for pair in values if pair[1] is not None),
        )
        for values in by_task.values()
        if all(pair[0] is not None and pair[1] is not None for pair in values)
    ]
    return rows, provenance


def _unknown(metric: str, provenance: Counter[str], stratum: str = "all") -> dict:
    return {
        "metric": metric,
        "stratum": stratum,
        "verdict": "unknown",
        "provenance": dict(sorted(provenance.items())),
        "why": "not every value is measured, so this criterion cannot pass",
    }


def _cost_per_verified_success(
    pairs: Sequence[tuple[Mapping, Mapping]], criterion: Mapping
) -> dict:
    """Judged on its point estimate: a resample with no success has no value to bound."""
    provenance: Counter[str] = Counter()
    totals = {"base": [0.0, 0.0], "treated": [0.0, 0.0]}
    for base, treated in pairs:
        for side, record in (("base", base), ("treated", treated)):
            cost, source = measure_of(record, "cost_usd")
            success, _ = measure_of(record, "verified_success")
            provenance[source if success is not None else UNKNOWN] += 1
            totals[side][0] += cost or 0.0
            totals[side][1] += success or 0.0
    metric = "cost_per_verified_success"
    if set(provenance) != {MEASURED}:
        return _unknown(metric, provenance)
    if not totals["base"][1] or not totals["treated"][1]:
        return {
            "metric": metric,
            "stratum": "all",
            "verdict": "unknown",
            "provenance": dict(provenance),
            "why": "an arm has no verified success, so it has no cost per success",
        }
    base_cost = totals["base"][0] / totals["base"][1]
    treated_cost = totals["treated"][0] / totals["treated"][1]
    change = treated_cost / base_cost - 1 if base_cost else None
    verdict = (
        "unknown"
        if change is None
        else ("pass" if change <= criterion["relative_margin"] else "fail")
    )
    return {
        "metric": metric,
        "stratum": "all",
        "verdict": verdict,
        "baseline_usd": base_cost,
        "treatment_usd": treated_cost,
        "relative_change": change,
        "relative_margin": criterion["relative_margin"],
        "provenance": dict(provenance),
    }


def evaluate_non_regression(
    pairs: Sequence[tuple[Mapping, Mapping]], criterion: Mapping, stats: Mapping
) -> dict:
    """``pass``: the interval's worse end is inside the margin; ``fail``: it all lies past."""
    metric, stratum = criterion["metric"], criterion["stratum"]
    if metric == "cost_per_verified_success":
        return _cost_per_verified_success(pairs, criterion)
    rows, provenance = _per_task_rows(pairs, metric, stratum)
    if set(provenance) != {MEASURED} or not rows:
        return _unknown(metric, provenance, stratum)
    relative = "relative_margin" in criterion
    statistic = relative_change if relative else mean_difference
    margin = criterion["relative_margin"] if relative else criterion["margin"]
    point = statistic(rows)
    interval = percentile_interval(rows, statistic, stats)
    if point is None or interval is None:
        return {**_unknown(metric, provenance, stratum), "why": "the baseline here is zero"}
    low, high = interval
    if metric in HIGHER_IS_BETTER:
        verdict = "pass" if low >= -margin else "fail" if high < -margin else "inconclusive"
    else:
        verdict = "pass" if high <= margin else "fail" if low > margin else "inconclusive"
    return {
        "metric": metric,
        "stratum": stratum,
        "verdict": verdict,
        "effect": point,
        "interval": [low, high],
        "margin": margin,
        "relative": relative,
        "tasks": len(rows),
        "provenance": dict(provenance),
    }


def evaluate_primary(
    pairs: Sequence[tuple[Mapping, Mapping]], primary: Mapping, stats: Mapping
) -> dict:
    """``pass``: at least the effect that matters, and the interval excludes none at all."""
    metric, stratum = primary["metric"], primary["stratum"]
    rows, provenance = _per_task_rows(pairs, metric, stratum)
    if set(provenance) != {MEASURED} or not rows:
        return _unknown(metric, provenance, stratum)
    point = mean_difference(rows)
    low, high = percentile_interval(rows, mean_difference, stats)
    minimum = primary["min_effect"]
    verdict = (
        "pass" if point >= minimum and low > 0 else "fail" if high < minimum else ("inconclusive")
    )
    return {
        "metric": metric,
        "stratum": stratum,
        "verdict": verdict,
        "effect": point,
        "interval": [low, high],
        "min_effect": minimum,
        "tasks": len(rows),
        "provenance": dict(provenance),
    }


def _measured(measure: Mapping) -> float | None:
    """A Memory measure's value when it is a measurement, and of a possible amount."""
    value = measure.get("value")
    if (
        measure.get("provenance") != MEASURED
        or isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not 0 <= value < math.inf
    ):
        return None
    return value


def _memory_gate(gate: str, treated: Sequence[Mapping], ceilings: Mapping[str, float]) -> dict:
    """``fail`` on any measured value past its ceiling, whatever else is not known of the
    arm: an unknown elsewhere leaves that value past it. Otherwise ``unknown`` unless every
    run that left evidence measured every value."""
    read = [
        (record["run_evidence"], _measured(record["memory"][key]), ceiling)
        for record in treated
        for key, ceiling in ceilings.items()
    ]
    over = sum(value is not None and value > ceiling for _, value, ceiling in read)
    if over:
        return {"gate": gate, "verdict": "fail", "count": over}
    ran = [value for evidence, value, _ in read if evidence]
    if not ran or any(value is None for value in ran):
        return {"gate": gate, "verdict": "unknown", "why": "not measured on this arm"}
    return {"gate": gate, "verdict": "pass", "count": 0}


def _hard_gate(
    gate: str,
    treated: Sequence[Mapping],
    treatment: str,
    harness: Mapping | None,
    prereg: Mapping,
) -> dict:
    """One gate over ``treated``: every record of the arm's planned runs, paired or not.

    Harm seen in any of them fails it. Leaving a pair takes a run out of the statistics,
    not out of what was seen. A clean verdict needs every run that left evidence, so an
    exclusion cannot make a run's check unnecessary; a planned run that never started
    has nothing to check.
    """
    if gate == "safety_failures":
        found = sum(len(record["safety_failures"]) for record in treated)
        if found:
            return {"gate": gate, "verdict": "fail", "count": found}
        ran = [record for record in treated if record["run_evidence"]]
        unassessed = sum(not record["safety_assessed"] for record in ran)
        if unassessed or not ran:
            return {
                "gate": gate,
                "verdict": "unknown",
                "why": f"{unassessed} run(s) have no affirmative safety check",
            }
        return {"gate": gate, "verdict": "pass", "count": 0}
    if gate == "stale_memory_leaks":
        return _memory_gate(gate, treated, {"stale_leaks": 0})
    if gate == "memory_overhead":
        ceilings = prereg["ceilings"]["memory_overhead"]
        return _memory_gate(
            gate, treated, {key: ceilings[limit] for key, limit in MEMORY_OVERHEAD.items()}
        )
    # crash_recovery_failures: a deterministic test of the control plane on this
    # arm, from a fixture report (section 5). A real run cannot be made to crash
    # at a chosen window without changing the pair it belongs to.
    covered = (harness or {}).get("harness_correctness", {}).get("arms_covered", [])
    if treatment not in covered:
        return {"gate": gate, "verdict": "unknown", "why": "no fixture report covers this arm"}
    ok = harness["harness_correctness"]["status"] == "pass"
    return {"gate": gate, "verdict": "pass" if ok else "fail"}


def evaluate_contrast(
    baseline: str,
    treatment: str,
    comparison: Mapping,
    records: Sequence[Mapping],
    manifest: Mapping,
    prereg: Mapping,
    harness: Mapping | None = None,
) -> dict:
    """One contrast's decision, from real records only; see the protocol's section 5."""
    arms = arms_by_id(manifest)
    result: dict = {"baseline": baseline, "treatment": treatment}
    missing_arms = [arm for arm in (baseline, treatment) if arms[arm]["status"] != "available"]
    if missing_arms:
        needs = sorted({n for arm in missing_arms for n in arms[arm]["requires"]})
        return {
            **result,
            "decision": NOT_EVALUATED,
            "why": f"{', '.join(missing_arms)} needs #{', #'.join(str(n) for n in needs)}",
        }
    real = [r for r in records if r["evidence"] == REAL]
    if not real:
        return {**result, "decision": NOT_EVALUATED, "why": "no real run has been made"}
    repeated = _repeated_runs(real, (baseline, treatment))
    if repeated:
        # Never one attempt in place of another: the plan has one run per arm, task and
        # seed, and which of two to keep is exactly the choice nobody may make afterwards.
        return {
            **result,
            "decision": NOT_EVALUATED,
            "why": f"{len(repeated)} planned run(s) have more than one record",
            "repeated_runs": repeated,
        }
    runs = {(r["arm"], r["task"], r["seed"]): r for r in real}
    expected = [(t["id"], seed) for t in manifest["tasks"] for seed in prereg["sample"]["seeds"]]
    pairs, excluded, missing = [], [], []
    for task, seed in expected:
        base, treated = runs.get((baseline, task, seed)), runs.get((treatment, task, seed))
        if base is None or treated is None:
            missing.append([task, seed])
        elif base["outcome"] in EXCLUDED_OUTCOMES or treated["outcome"] in EXCLUDED_OUTCOMES:
            excluded.append(
                {
                    "task": task,
                    "seed": seed,
                    "baseline": base["outcome"],
                    "treatment": treated["outcome"],
                    "reason": base["stopped_reason"] or treated["stopped_reason"],
                }
            )
        else:
            pairs.append((base, treated))
    result |= {"pairs": len(pairs), "planned_pairs": len(expected), "excluded": excluded}
    result["missing"] = missing
    paired = [record for pair in pairs for record in pair]
    # Every planned run with a record. Leaving a pair takes a run out of the statistics, not
    # out of what it spent or was shown.
    observed = [
        runs[(arm, task, seed)]
        for task, seed in expected
        for arm in (baseline, treatment)
        if (arm, task, seed) in runs
    ]
    result["ceiling_stopped"] = _ceiling_stopped(observed, prereg)
    result["settings_unknown"] = [
        _run_key(r) for r in paired if r["settings_evidence"]["status"] == "unknown"
    ]
    result["incomplete_evidence"] = [
        _run_key(r)
        for r in paired
        if r["run_evidence"] and (r["journal_problems"] or r["unknown_counts"])
    ]

    invalid = []
    if {r["manifest_digest"] for r in real} != {digest(manifest)}:
        invalid.append("a record was made under another manifest")
    if {r["preregistration_digest"] for r in real} != {digest(prereg)}:
        invalid.append("a record was made under another pre-registration")
    if any(
        base["settings_digest"] != treated["settings_digest"]
        for base, treated in pairs
        if base["settings_digest"] and treated["settings_digest"]
    ):
        invalid.append("a pair's settings are not equal")
    if any(r["settings_evidence"]["status"] == "mismatch" for r in paired):
        invalid.append("a run's own records show other settings than its record states")
    tasks = tasks_by_id(manifest)
    if any(
        r["run_evidence"]
        and r["prior_experience"] != seeded_digest(manifest, r["arm"], tasks[r["task"]])
        for r in paired
    ):
        invalid.append("a run did not start with its task's prior experience")
    if any(r["counts"]["hidden_leaks"] for r in observed):
        invalid.append("a hidden test or a pinned commit reached an agent")
    if invalid:
        return {**result, "decision": NOT_EVALUATED, "why": "; ".join(invalid)}

    stats = prereg["statistics"]
    treated_runs = [record for record in observed if record["arm"] == treatment]
    result["hard_gates"] = [
        _hard_gate(gate, treated_runs, treatment, harness, prereg)
        for gate in comparison["hard_gates"]
    ]
    result["primary"] = evaluate_primary(pairs, comparison["primary"], stats) if pairs else None
    result["non_regression"] = [
        evaluate_non_regression(pairs, criterion, stats)
        for criterion in comparison["non_regression"]
    ]
    result["per_task"] = _per_task_table(pairs, excluded, missing, manifest, prereg)
    result["decision"], result["why"] = _decide(result, stats, len(expected))
    return result


def _run_key(record: Mapping) -> list:
    return [record["arm"], record["task"], record["seed"]]


def _repeated_runs(real: Sequence[Mapping], arms: Sequence[str]) -> list[dict]:
    """Each (arm, task, seed) of these arms with more than one record, with their outcomes."""
    held: dict[tuple, list[str]] = {}
    for record in real:
        if record["arm"] in arms:
            held.setdefault(tuple(_run_key(record)), []).append(record["outcome"])
    return [
        {"arm": arm, "task": task, "seed": seed, "outcomes": sorted(outcomes)}
        for (arm, task, seed), outcomes in sorted(held.items())
        if len(outcomes) > 1
    ]


def _ceiling_stopped(observed: Sequence[Mapping], prereg: Mapping) -> list[dict]:
    """Each run at a registered ceiling: stopped there, or measured past it unsaid, by the part
    known of a total that is not. An exclusion or a lost counterpart does not take back what
    a run spent."""
    ceilings = prereg["ceilings"]
    found = []
    for record in observed:
        cost, wall = record["usage"]["cost_usd"], record["volatile"]["wall_clock_seconds"]
        known_cost = cost.get("known_part") if cost["value"] is None else cost["value"]
        known_wall = wall.get("known_part") if wall["value"] is None else wall["value"]
        if record["outcome"] in CEILING_OUTCOMES:
            ceiling = record["outcome"]
        elif known_cost is not None and known_cost > ceilings["per_run_usd"][record["arm"]]:
            ceiling = "cost_ceiling"
        elif known_wall is not None and known_wall > ceilings["per_run_wall_clock_hours"] * 3600:
            ceiling = "wall_clock_ceiling"
        else:
            continue
        found.append(
            {
                "arm": record["arm"],
                "task": record["task"],
                "seed": record["seed"],
                "ceiling": ceiling,
                "cost_usd": known_cost,
                "wall_clock_seconds": known_wall,
                "reason": record["stopped_reason"],
            }
        )
    return found


def _per_task_table(
    pairs: Sequence[tuple[Mapping, Mapping]],
    excluded: Sequence[Mapping],
    missing: Sequence[Sequence],
    manifest: Mapping,
    prereg: Mapping,
) -> list[dict]:
    """Each task's seeds, successes and failures on each side, what is not known of them,
    and how far its seeds disagree, so one outlier or one lost task can be seen."""
    table = {
        task["id"]: {
            "task": task["id"],
            "memory_class": task["memory_class"],
            "planned_seeds": len(prereg["sample"]["seeds"]),
            "seeds": 0,
            "excluded_seeds": 0,
            "missing_seeds": 0,
            "baseline_verified": 0,
            "treatment_verified": 0,
            "baseline_outcomes": Counter(),
            "treatment_outcomes": Counter(),
            "seed_differences": [],
            "unknown_usage": Counter(),
        }
        for task in manifest["tasks"]
    }
    for base, treated in pairs:
        row = table[base["task"]]
        row["seeds"] += 1
        row["baseline_verified"] += base["outcome"] == VERIFIED
        row["treatment_verified"] += treated["outcome"] == VERIFIED
        row["baseline_outcomes"][base["outcome"]] += 1
        row["treatment_outcomes"][treated["outcome"]] += 1
        row["seed_differences"].append(
            float(treated["outcome"] == VERIFIED) - float(base["outcome"] == VERIFIED)
        )
        for key in MEASURES:
            row["unknown_usage"][key] += sum(
                record["usage"][key]["provenance"] == UNKNOWN for record in (base, treated)
            )
    for entry in excluded:
        table[entry["task"]]["excluded_seeds"] += 1
    for task, _ in missing:
        table[task]["missing_seeds"] += 1
    rows = []
    for row in table.values():
        differences = sorted(row.pop("seed_differences"))
        rows.append(
            {
                **row,
                "baseline_outcomes": dict(sorted(row["baseline_outcomes"].items())),
                "treatment_outcomes": dict(sorted(row["treatment_outcomes"].items())),
                "unknown_usage": dict(sorted(row["unknown_usage"].items())),
                "effect": statistics.fmean(differences) if differences else None,
                "seed_range": [differences[0], differences[-1]] if differences else None,
            }
        )
    return sorted(rows, key=lambda row: row["task"])


def _decide(result: Mapping, stats: Mapping, planned: int) -> tuple[str, str]:
    """NO_GO on evidence of harm, first, whatever the sample; then an incomplete sample
    is inconclusive, before any primary effect is read; GO only with everything measured
    and within bounds. Harm seen on a run no pair holds still comes first, so only then
    does having no pair at all leave nothing to decide, unless a ceiling stopped the plan."""
    failed = [g["gate"] for g in result["hard_gates"] if g["verdict"] == "fail"]
    if failed:
        return NO_GO, f"hard gate failed: {', '.join(failed)}"
    if result["primary"] is None:
        if result["ceiling_stopped"]:
            return INCONCLUSIVE, f"{len(result['ceiling_stopped'])} run(s) reached a ceiling"
        return NOT_EVALUATED, "no pair of the plan has both runs"
    regressed = [c["metric"] for c in result["non_regression"] if c["verdict"] == "fail"]
    if regressed:
        return NO_GO, f"past its non-regression margin: {', '.join(regressed)}"
    if result["missing"]:
        return INCONCLUSIVE, f"{len(result['missing'])} planned pair(s) have no record"
    if len(result["excluded"]) > stats["max_excluded_pair_share"] * planned:
        return INCONCLUSIVE, f"{len(result['excluded'])} pair(s) excluded, over the bound"
    if result["ceiling_stopped"]:
        return INCONCLUSIVE, f"{len(result['ceiling_stopped'])} run(s) reached a ceiling"
    if result["settings_unknown"]:
        return INCONCLUSIVE, f"the settings of {len(result['settings_unknown'])} run(s) are unknown"
    if result["incomplete_evidence"]:
        return (
            INCONCLUSIVE,
            f"the evidence of {len(result['incomplete_evidence'])} run(s) is incomplete",
        )
    if result["primary"]["verdict"] == "fail":
        return NO_GO, "the interval lies below the smallest effect that matters"
    open_items = [
        *(g["gate"] for g in result["hard_gates"] if g["verdict"] != "pass"),
        *(c["metric"] for c in result["non_regression"] if c["verdict"] != "pass"),
    ]
    if open_items:
        return INCONCLUSIVE, f"not shown within bounds: {', '.join(open_items)}"
    if result["primary"]["verdict"] != "pass":
        return INCONCLUSIVE, "the primary effect is neither shown nor ruled out"
    return GO, "the primary effect is shown and every bound holds"


# -- reports ---------------------------------------------------------------------------------


def _uncertainty(records: Sequence[Mapping]) -> dict:
    """Failures and what is not known: printed with every report, not on request."""
    outcomes = Counter(r["outcome"] for r in records)
    return {
        "runs": len(records),
        "tasks": len({r["task"] for r in records}),
        "seeds_per_task": sorted({r["seed"] for r in records}),
        "outcomes": dict(sorted(outcomes.items())),
        "not_verified": sum(r["outcome"] != VERIFIED for r in records),
        "human_blocked_interventions": sum(
            r["counts"]["human_blocked_interventions"] for r in records
        ),
        "timeouts": sum(r["counts"]["timeouts"] for r in records),
        "truncations": sum(r["counts"]["truncations"] for r in records),
        "validation": dict(sorted(Counter(r["validation"] for r in records).items())),
        "usage_provenance": {
            key: dict(sorted(Counter(r["usage"][key]["provenance"] for r in records).items()))
            for key in MEASURES
        },
        "measured_cost_usd": _sum_by_provenance(records, MEASURED),
        "estimated_cost_usd": _sum_by_provenance(records, ESTIMATED),
        # Of runs whose total is unknown: what they are known to have spent, at least.
        "known_part_cost_usd": sum(r["usage"]["cost_usd"].get("known_part", 0.0) for r in records),
        "safety_failures": sum(len(r["safety_failures"]) for r in records),
        "safety_unassessed": sum(not r["safety_assessed"] for r in records),
        "settings_not_established": sum(
            r["settings_evidence"]["status"] != "established" for r in records
        ),
        "incomplete_run_logs": sum(
            bool(r["journal_problems"]) for r in records if r["run_evidence"]
        ),
        "unknown_counts": dict(
            sorted(Counter(key for r in records for key in r["unknown_counts"]).items())
        ),
    }


def _sum_by_provenance(records: Sequence[Mapping], provenance: str) -> float | None:
    """A cost total of one provenance; ``None`` when no run has one, never a zero."""
    values = [
        r["usage"]["cost_usd"]["value"]
        for r in records
        if r["usage"]["cost_usd"]["provenance"] == provenance
    ]
    return sum(values) if values else None


def build_report(
    records: Sequence[Mapping],
    manifest: Mapping,
    prereg: Mapping,
    *,
    harness_checks: Mapping | None = None,
    harness_report: Mapping | None = None,
) -> dict:
    """Harness correctness and model quality, each from its own runs, never summed."""
    for record in records:
        problems = record_problems(record)
        if problems:
            raise EvaluationError(f"{record.get('task')}: {'; '.join(problems)}")
    fixture = [deterministic_view(r) for r in records if r["evidence"] == FIXTURE]
    real = [r for r in records if r["evidence"] == REAL]
    harness: dict = {
        "says": "fixture runs check this harness and the controller path it drives; no model "
        "ran, so they are no evidence of model quality",
        "status": "not_run",
        "arms_covered": sorted({r["arm"] for r in fixture}),
        "uncertainty": _uncertainty(fixture),
        "runs": sorted(fixture, key=lambda r: (r["task"], r["seed"])),
    }
    if harness_checks is not None:
        failing = sorted(
            f"{run}: {check}"
            for run, checks in harness_checks.items()
            for check, ok in checks.items()
            if not ok
        )
        harness["status"] = "fail" if failing else "pass"
        harness["failing_checks"] = failing
        harness["checks"] = {
            run: dict(sorted(checks.items())) for run, checks in harness_checks.items()
        }
    comparisons = {
        name: [
            evaluate_contrast(
                contrast["baseline"],
                contrast["treatment"],
                comparison,
                real,
                manifest,
                prereg,
                harness_report,
            )
            for contrast in comparison["contrasts"]
        ]
        for name, comparison in prereg["comparisons"].items()
    }
    return {
        "schema": REPORT_SCHEMA,
        "manifest_digest": digest(manifest),
        "preregistration_digest": digest(prereg),
        "harness_correctness": harness,
        "model_quality": {
            "says": "real runs only: measured outcomes decide, estimates are shown apart",
            "real_runs": len(real),
            "uncertainty": _uncertainty(real),
            "runs": sorted(real, key=lambda r: (r["task"], r["seed"], r["arm"], r["outcome"])),
            "comparisons": comparisons,
        },
        "not_evidence_for_autoforge": manifest["external_results_not_evidence"],
    }


def render(report: Mapping) -> str:
    """The report for a reader: what it can and cannot say comes first."""
    harness, quality = report["harness_correctness"], report["model_quality"]
    lines = [
        f"manifest {report['manifest_digest'][:12]}  "
        f"pre-registration {report['preregistration_digest'][:12]}",
        "",
        "HARNESS CORRECTNESS (fixture runs; no model ran; no evidence of model quality)",
        f"  status: {harness['status']}",
    ]
    lines += [f"  FAILED CHECK {check}" for check in harness.get("failing_checks", [])]
    lines += _render_uncertainty(harness["uncertainty"])
    for run in harness["runs"]:
        reasons = f" ({', '.join(run['unverified_reasons'])})" if run["unverified_reasons"] else ""
        lines.append(
            f"  {run['task']} seed {run['seed']}: {run['outcome']}{reasons}; "
            f"validation {run['validation']}"
        )
    lines += ["", f"MODEL QUALITY (real runs: {quality['real_runs']})"]
    lines += _render_uncertainty(quality["uncertainty"])
    for run in quality["runs"]:
        lines += _render_real_run(run)
    for name, contrasts in quality["comparisons"].items():
        for contrast in contrasts:
            lines.append(
                f"  {name}: {contrast['treatment']} vs {contrast['baseline']}: "
                f"{contrast['decision']} ({contrast['why']})"
            )
            lines += _render_contrast(contrast)
    lines += ["", "NOT EVIDENCE FOR AUTOFORGE"]
    lines += [f"  {item}" for item in report["not_evidence_for_autoforge"]]
    return "\n".join(lines)


def _shown_measure(measure: Mapping) -> str:
    """A value with where it came from; of an unknown total, what is known of it, apart."""
    part = f", at least {measure['known_part']}" if "known_part" in measure else ""
    return f"{measure['value']} ({measure['provenance']}{part})"


def _render_real_run(run: Mapping) -> list[str]:
    """One real run in full: its outcome and reasons, what it spent and what is not known."""
    reasons = f" ({', '.join(run['unverified_reasons'])})" if run["unverified_reasons"] else ""
    certificate = run["certificate"]
    held = "held" if certificate["held"] else "none"
    usage = run["usage"]
    lines = [
        f"  {run['task']} seed {run['seed']} {run['arm']}: {run['outcome']}{reasons}; validation "
        f"{run['validation']}; certificate {held}; phase {run['phase'] or '(none)'}",
        f"    cost {_shown_measure(usage['cost_usd'])}; tokens "
        f"{_shown_measure(usage['context_tokens'])}; duplicate exploration "
        f"{_shown_measure(usage['duplicate_exploration'])}; wall clock "
        f"{_shown_measure(run['volatile']['wall_clock_seconds'])}",
        f"    oracle {run['oracle'].get(run['final_candidate'], 'not_run')}; pristine checkers "
        f"{run['pristine_checkers']['status']}; settings {run['settings_evidence']['status']}; "
        f"safety {'checked' if run['safety_assessed'] else 'unknown'}; run log {run['journal']}",
    ]
    details = [
        ("stopped", run["stopped_reason"]),
        ("certificate does not apply", run["certificate"]["problem"]),
        ("altered checkers", ", ".join(run["altered_checkers"])),
        ("pristine failures", ", ".join(run["pristine_checkers"]["failing_modules"])),
        ("settings", "; ".join(run["settings_evidence"]["problems"])),
        ("run log", "; ".join(run["journal_problems"])),
        ("counts not known", ", ".join(run["unknown_counts"])),
        ("safety failures", "; ".join(run["safety_failures"])),
    ]
    return lines + [f"    {label}: {text}" for label, text in details if text]


def _render_contrast(contrast: Mapping) -> list[str]:
    """A decision's evidence: each criterion, what left the sample, and every task."""
    lines = []
    primary = contrast.get("primary")
    if primary:
        lines.append(f"    primary {_render_criterion(primary)}")
    lines += [
        f"    non-regression {_render_criterion(c)}" for c in contrast.get("non_regression", ())
    ]
    lines += [
        f"    gate {gate['gate']}: {gate['verdict']}"
        + (f" ({gate['why']})" if "why" in gate else "")
        for gate in contrast.get("hard_gates", ())
    ]
    lines += [
        f"    excluded {e['task']} seed {e['seed']}: {e['baseline']} / {e['treatment']} "
        f"({e['reason']})"
        for e in contrast.get("excluded", ())
    ]
    lines += [
        f"    no record {task} seed {seed}"  # a planned pair without both runs
        for task, seed in contrast.get("missing", ())
    ]
    lines += [
        f"    {c['ceiling']} {c['arm']} on {c['task']} seed {c['seed']}: cost {c['cost_usd']}, "
        f"wall clock {c['wall_clock_seconds']} ({c['reason'] or 'measured past it'})"
        for c in contrast.get("ceiling_stopped", ())
    ]
    lines += [
        f"    repeated {r['arm']} on {r['task']} seed {r['seed']}: {', '.join(r['outcomes'])}"
        for r in contrast.get("repeated_runs", ())
    ]
    for row in contrast.get("per_task", ()):
        lines.append(
            f"    task {row['task']} ({row['memory_class']}): {row['seeds']} of "
            f"{row['planned_seeds']} seeds, {row['excluded_seeds']} excluded, "
            f"{row['missing_seeds']} missing; verified {row['baseline_verified']} vs "
            f"{row['treatment_verified']}; effect {row['effect']}, seeds {row['seed_range']}; "
            f"outcomes {row['baseline_outcomes']} vs {row['treatment_outcomes']}; "
            f"unknown usage {row['unknown_usage']}"
        )
    return lines


def _render_criterion(criterion: Mapping) -> str:
    bound = criterion.get("min_effect", criterion.get("margin", criterion.get("relative_margin")))
    effect = criterion.get("effect", criterion.get("relative_change"))
    return (
        f"{criterion['metric']} on {criterion['stratum']}: {criterion['verdict']}, effect "
        f"{effect}, interval {criterion.get('interval')}, bound {bound}, provenance "
        f"{criterion['provenance']}" + (f" ({criterion['why']})" if "why" in criterion else "")
    )


def _render_uncertainty(uncertainty: Mapping) -> list[str]:
    if not uncertainty["runs"]:
        return ["  no runs"]
    return [
        f"  runs {uncertainty['runs']} over {uncertainty['tasks']} tasks, seeds "
        f"{uncertainty['seeds_per_task']}; not verified {uncertainty['not_verified']}; "
        f"human interventions {uncertainty['human_blocked_interventions']}; "
        f"timeouts {uncertainty['timeouts']}; truncations {uncertainty['truncations']}",
        f"  outcomes {uncertainty['outcomes']}; validation {uncertainty['validation']}",
        f"  usage {uncertainty['usage_provenance']}; measured cost "
        f"{uncertainty['measured_cost_usd']}; estimated cost {uncertainty['estimated_cost_usd']}; "
        f"known part of unknown costs {uncertainty['known_part_cost_usd']}",
        f"  safety failures {uncertainty['safety_failures']}; safety not checked "
        f"{uncertainty['safety_unassessed']}; settings not established "
        f"{uncertainty['settings_not_established']}; incomplete run logs "
        f"{uncertainty['incomplete_run_logs']}; counts not known {uncertainty['unknown_counts']}",
    ]


# -- the command line ------------------------------------------------------------------------


def plan_runs(name: str, manifest: Mapping, prereg: Mapping) -> dict:
    """What a comparison would run, in its predeclared order. Launches and writes nothing.

    Which arm of a pair goes first is a digest of the pair, fixed before any
    run, so no arm is always the one that runs against the fresher service.
    """
    comparison = prereg["comparisons"][name]
    arms = arms_by_id(manifest)
    wanted = comparison_arms(comparison)
    runnable = [arm for arm in wanted if arms[arm]["status"] == "available"]
    runs = []
    for task in manifest["tasks"]:
        for seed in prereg["sample"]["seeds"]:
            order = sorted(runnable, key=lambda arm: digest([name, task["id"], seed, arm]))
            runs += [
                {
                    "task": task["id"],
                    "seed": seed,
                    "arm": arm,
                    "base_tree": task["base_tree"],
                    "validation_commands": [validation_command(manifest, task)],
                    "ceiling_usd": prereg["ceilings"]["per_run_usd"][arm],
                    # What this arm's Memory holds when the run starts; nothing else does.
                    "prior_experience": (
                        task["prior_experience"]["lesson"]
                        if arms[arm]["seeded_experience"]
                        else None
                    ),
                }
                for arm in order
            ]
    return {
        "comparison": name,
        "cost_ceiling_usd": cost_ceiling_usd(prereg, name),
        "not_evaluated": {
            arm: arms[arm]["requires"] for arm in wanted if arms[arm]["status"] != "available"
        },
        "runs": runs,
    }


def _check_command(manifest: Mapping, prereg: Mapping) -> int:
    problems = [*manifest_problems(manifest), *preregistration_problems(prereg, manifest)]
    pinned = load_config_file(str(REAL_RUN_CONFIG))
    config_digest = settings_digest(pinned)
    if config_digest != manifest["equalized_settings"]["digest"]:
        problems.append(f"the real-run configuration's digest is {config_digest}")
    if pinned.validation.commands != [manifest["validation_command_prefix"]]:
        problems.append("the real-run configuration's validation is not the manifest's prefix")
    for problem in problems:
        print(f"problem: {problem}")
    print(f"manifest {digest(manifest)}\npre-registration {digest(prereg)}")
    return 1 if problems else 0


def _verify_pins_command(args: argparse.Namespace, manifest: Mapping) -> int:
    from autoforge.config import default_config

    repo = str(Path(args.git).resolve())
    problems = [p for task in manifest["tasks"] for p in verify_pin_history(repo, task)]
    for problem in problems:
        print(f"problem: {problem}")
    cfg = load_config_file(args.config) if args.config else default_config()
    if args.run_oracle:
        results = {
            task["id"]: verify_pin_oracle(repo, manifest, task, cfg) for task in manifest["tasks"]
        }
        print(json.dumps(results, indent=2, sort_keys=True))
        problems += [
            task
            for task, result in results.items()
            if result
            != {
                "base": "fail",
                "reference": "pass",
                "coupled_to_new_names": False,
                "lesson": "pass",
            }
        ]
    if args.base_suite:
        suites = {
            task["id"]: verify_base_suite(repo, manifest, task, cfg, args.workers)
            for task in manifest["tasks"]
        }
        print(json.dumps(suites, indent=2, sort_keys=True))
        problems += [task for task, suite in suites.items() if suite["status"] != "pass"]
    if args.reference_suite:
        suites = {
            task["id"]: verify_reference_suite(repo, manifest, task, cfg, args.workers)
            for task in manifest["tasks"]
        }
        print(json.dumps(suites, indent=2, sort_keys=True))
        problems += [task for task, suite in suites.items() if suite["status"] != "pass"]
    return 1 if problems else 0


def _collect_command(args: argparse.Namespace, manifest: Mapping, prereg: Mapping) -> int:
    """Score one real run. Asks for the opt-in, the approval, and a disposable repository."""
    if os.environ.get(OPT_IN) != "1":
        print(f"collect scores a real run: set {OPT_IN}=1 (docs/evaluation-protocol.md)")
        return 2
    if not args.approval.strip():
        print("collect needs --approval: where the separate approval of real runs is recorded")
        return 2
    task = tasks_by_id(manifest).get(args.task)
    if task is None or args.arm not in arms_by_id(manifest):
        print("collect: unknown --task or --arm")
        return 2
    if args.seed != SMOKE_SEED and args.seed not in prereg["sample"]["seeds"]:
        print("collect: this seed is neither in the plan nor a smoke run's")
        return 2
    if not args.state_dir and args.stopped_by:
        record = not_run_record(
            task,
            args.arm,
            args.seed,
            args.stopped_by,
            args.reason,
            digest(manifest),
            digest(prereg),
        )
    else:
        absent = [
            flag
            for flag, value in (
                ("--state-dir", args.state_dir),
                ("--repo", args.repo),
                ("--base-commit", args.base_commit),
                ("--oracle-repo", args.oracle_repo),
            )
            if not value
        ]
        if absent:
            print(f"collect needs {', '.join(absent)}, or --stopped-by with a --reason")
            return 2
        if args.stopped_by:
            check_stopped(args.stopped_by, args.reason)
        cfg = load_config_file(args.config)
        config_file = absolute_spelling(args.config)
        state_dir = Path(args.state_dir).resolve()
        repo = str(Path(args.repo).resolve())
        problems = []
        state = load_state(state_dir / "state.json")
        if state.repository in manifest["forbidden_repositories"]:
            problems.append(f"{state.repository} is no disposable repository")
        if _git(repo, "rev-parse", f"{args.base_commit}^{{tree}}") != task["base_tree"]:
            problems.append("the base commit's tree is not the task's pinned tree")
        if settings_digest(cfg) != settings_digest(task_config(manifest, task)):
            problems.append("the configuration is not this task's pinned one")
        if not cfg.sandbox.enabled:
            problems.append("a real run's oracle needs sandbox.mode: required")
        read, bad, journal = read_journal(state_dir, state.run_id)
        recorded = settings_evidence(
            state,
            _agent_lines(admitted_lines(state_dir, state.run_id, read)[0]),
            cfg,
            not run_log_problems(state_dir, state.run_id, read, bad, journal),
            config_file=config_file,
        )
        if recorded["status"] == "mismatch":
            problems += recorded["problems"]
        if problems:
            for problem in problems:
                print(f"collect refuses: {problem}")
            return 2
        overlay = hidden_overlay(args.oracle_repo, task)
        command = oracle_command(manifest, task)
        oracle_repo = str(Path(args.oracle_repo).resolve())
        denied = (str(state_dir), repo, oracle_repo)
        record = score_run(
            RunInput(
                evidence=REAL,
                task=task,
                arm=args.arm,
                seed=args.seed,
                state_dir=state_dir,
                repo=repo,
                base_commit=args.base_commit,
                live_head=args.live_head,
                cfg=cfg,
                oracle=lambda commit: (
                    run_oracle(repo, commit, overlay, command, cfg, denied_roots=denied).status
                ),
                manifest_digest=digest(manifest),
                preregistration_digest=digest(prereg),
                safety_failures=tuple(args.safety_failure),
                safety_evidence=args.safety_checked,
                pristine_checkers=lambda commit: run_oracle(
                    repo,
                    commit,
                    {},
                    pristine_command(manifest, task),
                    cfg,
                    denied_roots=denied,
                    pristine=(oracle_repo, task["reference_sha"]),
                ),
                stopped_by=args.stopped_by,
                stopped_reason=args.reason,
                config_file=config_file,
            )
        )
    record["approval"] = args.approval
    Path(args.out).write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{record['task']} seed {record['seed']} {record['arm']}: {record['outcome']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tests.evaluation", description=__doc__.split("\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("check", help="validate the pinned files and print their digests")
    plan = commands.add_parser("plan", help="print a comparison's runs; launches nothing")
    plan.add_argument("--comparison", required=True)
    pins = commands.add_parser("verify-pins", help="check the tasks against a clone's history")
    pins.add_argument("--git", required=True)
    pins.add_argument("--run-oracle", action="store_true", help="the hidden tests, both ends")
    pins.add_argument(
        "--base-suite", action="store_true", help="each task's validation at its base"
    )
    pins.add_argument(
        "--reference-suite",
        action="store_true",
        help="each task's validation at its reference: what the pristine checkers need",
    )
    pins.add_argument("--config", default="", help="the configuration to launch under")
    pins.add_argument(
        "--workers", type=int, default=0, help="pytest-xdist workers for --base-suite"
    )
    collect = commands.add_parser("collect", help="score one finished real run, read only")
    for name in ("--task", "--arm", "--out", "--approval"):
        collect.add_argument(name, required=True)
    collect.add_argument("--seed", type=int, required=True)
    for name in ("--state-dir", "--repo", "--base-commit", "--live-head", "--oracle-repo"):
        collect.add_argument(name, default="")
    collect.add_argument("--config", default=str(REAL_RUN_CONFIG))
    collect.add_argument("--stopped-by", choices=STOPPED_BY, default="")
    collect.add_argument("--reason", default="")
    collect.add_argument("--safety-failure", action="append", default=[])
    collect.add_argument(
        "--safety-checked", default="", help="where the operator's check of this run is recorded"
    )
    report = commands.add_parser("report", help="decide over run records and print the report")
    report.add_argument("records", nargs="*")
    report.add_argument("--harness-report", default="", help="a fixture report's JSON")
    report.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    manifest, prereg = load_manifest(), load_preregistration()
    if args.command == "check":
        return _check_command(manifest, prereg)
    if args.command == "plan":
        if args.comparison not in prereg["comparisons"]:
            parser.error(f"--comparison must be one of {sorted(prereg['comparisons'])}")
        print(json.dumps(plan_runs(args.comparison, manifest, prereg), indent=2))
        return 0
    if args.command == "verify-pins":
        return _verify_pins_command(args, manifest)
    if args.command == "collect":
        try:
            return _collect_command(args, manifest, prereg)
        except AutoForgeError as exc:
            print(f"collect refuses: {exc}")
            return 2
    records = [read_json(Path(path)) for path in args.records]
    harness = read_json(Path(args.harness_report)) if args.harness_report else None
    built = build_report(records, manifest, prereg, harness_report=harness)
    print(json.dumps(built, indent=2, sort_keys=True) if args.json else render(built))
    return 0


if __name__ == "__main__":
    sys.exit(main())
