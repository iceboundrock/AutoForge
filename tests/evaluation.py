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
    uv run python -m tests.evaluation verify-pins --git . --run-oracle
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
``not_run``, never a pass.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import random
import re
import statistics
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from autoforge.child_env import (
    isolated_overrides,
    operator_home,
    overrides_directory,
    preflight,
)
from autoforge.config import AutoForgeConfig, load_config_file
from autoforge.errors import AutoForgeError
from autoforge.evidence import Outcome, definitions, parse_candidate
from autoforge.executor import ExecutionRequest, execute, select_environment
from autoforge.git_transport import local_git_request
from autoforge.premerge import export_commit_tree
from autoforge.safefs import SafeRoot
from autoforge.sandbox_policy import LaunchClass, LaunchFacts, NetworkPosition, describe_launch
from autoforge.state import AutoForgeState, load_state
from autoforge.transitions import Phase
from tests.stop_corpus import _journal_bytes, _open_dir, launch_kind, operator_kind

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
STOPPED_BY = (*EXCLUDED_OUTCOMES, "cost_ceiling")
OUTCOMES = (VERIFIED, UNVERIFIED, "blocked", "failed", "timeout", "incomplete", *STOPPED_BY)

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

GO, NO_GO, INCONCLUSIVE, NOT_EVALUATED = "GO", "NO_GO", "INCONCLUSIVE", "NOT_EVALUATED"
# The seed of a smoke run: scored like any run, and outside every plan, so no
# comparison ever pairs it.
SMOKE_SEED = 0

# The tools whose repeated call is exploring the same thing again.
READ_ONLY_TOOLS = frozenset({"read", "grep", "glob", "find", "ls"})
_PROGRESS_TOOL = re.compile(r"^\[[^\]]*\] (?P<tool>\S+)(?: (?P<detail>.+))?$")
_FULL_SHA = re.compile(r"[0-9a-f]{40}")
_COUPLED = re.compile(r"\b(ImportError|ModuleNotFoundError|AttributeError|NameError)\b")
_FAILED_MODULE = re.compile(r"^(?:FAILED|ERROR) (tests/[^\s:]+\.py)", re.M)


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
    """What must never reach the agent: each hidden test's name."""
    return [node.partition("::")[2] for node in task["hidden_tests"]]


def statement_leaks(task: Mapping) -> list[str]:
    """What of the answer a task's statement gives away, which the agent would read."""
    statement = task["statement"]
    found = [name for name in hidden_names(task) if name in statement]
    found += [sha[:7] for sha in (task["reference_sha"], task["base_sha"]) if sha[:7] in statement]
    return found


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
    return problems


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


def _git(repo: str, *args: str) -> str:
    """One hardened git read of ``repo``; ``""`` when git refuses it."""
    res = execute(local_git_request(["-C", repo, *args], timeout_seconds=120))
    return res.stdout.strip() if res.exit_code == 0 and not res.timed_out else ""


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
        except ValueError:
            bad += 1
            continue
        if isinstance(line, dict):
            lines.append(line)
        else:
            bad += 1
    return lines, bad, status


def _regular_files(directory: Path) -> list[str]:
    """Names of the regular files directly in ``directory``, links unfollowed, sorted."""
    try:
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries if e.is_file(follow_symlinks=False))
    except OSError:
        return []


def _subdirectories(directory: Path) -> list[str]:
    try:
        with os.scandir(directory) as entries:
            return sorted(e.name for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def _bounded_files(state_dir: Path, relpaths: Sequence[str]) -> list[bytes]:
    """Each file's bytes, read as a run log is: a link or an oversized file is left out."""
    root: SafeRoot = _open_dir(state_dir)
    try:
        read = (_journal_bytes(root, relpath) for relpath in relpaths)
        return [data for status, data in read if status == "read"]
    finally:
        root.close()


def _shown_to_agents(state_dir: Path, run_id: str) -> list[str]:
    """What the run's agents were shown: every launch's prompt and read context."""
    logs = state_dir / "logs" / run_id
    shown = [
        f"logs/{run_id}/{step}/prompt.md"
        for step in _subdirectories(logs)
        if "prompt.md" in _regular_files(logs / step)
    ]
    context = state_dir / "read-context" / run_id
    for launch in _subdirectories(context):
        shown += [
            f"read-context/{run_id}/{launch}/{name}" for name in _regular_files(context / launch)
        ]
    return shown


def count_leaks(state_dir: Path, run_id: str, markers: Sequence[str]) -> int:
    """How many files shown to an agent name something it must not know."""
    needles = [marker.encode() for marker in markers]
    return sum(
        any(needle in data for needle in needles)
        for data in _bounded_files(state_dir, _shown_to_agents(state_dir, run_id))
    )


def duplicate_exploration(state_dir: Path, run_id: str, provenance: str) -> dict:
    """Read-only tool calls that repeat one the run already made, across its launches.

    From the allow-listed detail each progress line shows, so a `Bash` `cat` is
    not seen. A run whose log shows no read-only call at all is ``unknown``, not
    zero: a provider whose progress names no tool shows none either.
    """
    logs = state_dir / "logs" / run_id
    relpaths = [
        f"logs/{run_id}/{step}/progress.log"
        for step in _subdirectories(logs)
        if "progress.log" in _regular_files(logs / step)
    ]
    seen: Counter[tuple[str, str]] = Counter()
    for data in _bounded_files(state_dir, relpaths):
        for line in data.decode("utf-8", "replace").splitlines():
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
        if result.get("phase") in ("ANALYZE_EXECUTE", "FIX", "REPLAN_REEXECUTE") and isinstance(
            head, str
        ):
            found.append(head)
    return found


def validation_status(state: AutoForgeState, cfg: AutoForgeConfig, commit: str) -> str:
    """``not_configured``, ``absent``, ``failed`` or ``passed``, from ValidationRecords alone.

    FULL evidence only (ADR 0010 D1, D2): the configured set's latest record for
    this exact commit. Nothing else a run leaves behind can make it ``passed``.
    """
    defs = definitions(cfg.validation.commands)
    if not defs:
        return "not_configured"
    outcomes: dict[str, Outcome] = {}
    for view in state.validation_evidence():
        if view.candidate.commit == commit:
            outcomes[view.definition_digest] = view.outcome
    held = [outcomes.get(d.digest) for d in defs]
    if any(outcome is None for outcome in held):
        return "failed" if any(o not in (None, Outcome.PASS) for o in held) else "absent"
    return "passed" if all(outcome is Outcome.PASS for outcome in held) else "failed"


def _wall_clock(state: AutoForgeState) -> float | None:
    try:
        start = datetime.fromisoformat(state.created_at)
        end = datetime.fromisoformat(state.updated_at)
    except ValueError:
        return None
    return max((end - start).total_seconds(), 0.0)


def _changed_paths(repo: str, base: str, head: str) -> list[str]:
    return _git(repo, "diff", "--name-only", "--no-renames", base, head).splitlines()


def touches_protected(changed: Sequence[str], protected: Sequence[str]) -> bool:
    return any(
        path == entry or (entry.endswith("/") and path.startswith(entry))
        for path in changed
        for entry in protected
    )


def _usage(agent_lines: Sequence[Mapping], key: str, provenance: str, scale: float) -> dict:
    """A launch-reported total, and how many launches reported none: never a partial sum."""
    summaries = [line.get("provider_summary") or {} for line in agent_lines]
    values = [s.get(key) for s in summaries]
    known = [v for v in values if isinstance(v, int) and not isinstance(v, bool)]
    if agent_lines and len(known) == len(values):
        return {"value": sum(known) * scale, "provenance": provenance}
    return {"value": None, "provenance": UNKNOWN}


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


def score_run(run: RunInput) -> dict:
    """The run's record, from its state file, run log, git and the oracle.

    Reads and launches nothing else. The state file is loaded by the
    controller's strict loader, the run log as ``tests/stop_corpus.py`` reads
    it, and only typed fields are read: no error text, no agent prose.
    """
    state = load_state(run.state_dir / "state.json")
    lines, bad, journal = read_journal(run.state_dir, state.run_id)
    agents = [line for line in lines if not operator_kind(line) and launch_kind(line) == "agent"]
    applied = sum(operator_kind(line) == "unblock-applied" for line in lines)
    provenance = MEASURED if run.evidence == REAL else SYNTHETIC

    certificate = state.review_certificate or {}
    certified = parse_candidate(certificate["candidate"]).commit if certificate else ""
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
    counts["journal_bad_lines"] = bad
    counts["hidden_leaks"] = count_leaks(
        run.state_dir, state.run_id, run.task.get("leak_markers") or hidden_names(run.task)
    )
    counts["lost_success"] = int(
        final_oracle != "pass" and any(oracle[c] == "pass" for c in candidates if c != final)
    )
    counts["false_oracle"] = int(bool(certified) and oracle.get(certified) == "fail")
    counts["stale_completed_nodes"] = int(bool(certified) and certified != final)
    if final:
        counts["checker_altered"] = int(
            touches_protected(
                _changed_paths(run.repo, run.base_commit, final), run.task["protected_paths"]
            )
        )
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
    if state.phase in (Phase.READY_FOR_MERGE, Phase.MERGE, Phase.DONE):
        if validation != "passed":
            reasons.append(f"validation_{validation}")
        if final_oracle != "pass":
            reasons.append("oracle_failed" if final_oracle == "fail" else f"oracle_{final_oracle}")
        if not certificate:
            reasons.append("no_certificate")
        elif certified != final:
            reasons.append("stale_certificate")
        if counts["checker_altered"]:
            reasons.append("checker_altered")
        if counts["hidden_leaks"]:
            reasons.append("hidden_test_leak")
        outcome = UNVERIFIED if reasons else VERIFIED
    else:
        outcome = _stop_outcome(state, agents)

    wall = _wall_clock(state)
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
        "phase": state.phase.value,
        "outcome": outcome,
        "unverified_reasons": sorted(reasons),
        "validation": validation,
        "certificate": {"held": bool(certificate), "candidate": certified},
        "final_candidate": final,
        "candidates": candidates,
        "oracle": oracle,
        "counts": counts,
        "usage": {
            "cost_usd": _usage(agents, "cost_micro_usd", provenance, 1e-6),
            "context_tokens": _usage(agents, "context_tokens", provenance, 1),
            "duplicate_exploration": duplicate_exploration(run.state_dir, state.run_id, provenance),
        },
        # An arm without Memory has neither; its collector reports them when it exists.
        "memory": {
            "stale_leaks": {"value": None, "provenance": NOT_APPLICABLE},
            "storage_bytes": {"value": None, "provenance": NOT_APPLICABLE},
            "projection_chars": {"value": None, "provenance": NOT_APPLICABLE},
            "latency_ms": {"value": None, "provenance": NOT_APPLICABLE},
        },
        "safety_failures": sorted(run.safety_failures),
        "stopped_reason": "",
        "journal": journal,
        # What differs between two runs of the same inputs; no reproducibility
        # check reads it (deterministic_view).
        "volatile": {
            "run_id": state.run_id,
            "wall_clock_seconds": {
                "value": wall,
                "provenance": provenance if wall is not None else UNKNOWN,
            },
        },
    }


def not_run_record(
    task: Mapping,
    arm: str,
    seed: int,
    stopped_by: str,
    reason: str,
    manifest_digest: str,
    preregistration_digest: str,
) -> dict:
    """A planned run an operator says did not produce a verdict, and why: never dropped."""
    if stopped_by not in STOPPED_BY:
        raise EvaluationError(f"--stopped-by must be one of {STOPPED_BY}")
    if not reason.strip():
        raise EvaluationError("a run that did not finish needs a stated reason")
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
        "phase": "",
        "outcome": stopped_by,
        "unverified_reasons": [],
        "validation": "absent",
        "certificate": {"held": False, "candidate": ""},
        "final_candidate": "",
        "candidates": [],
        "oracle": {},
        "counts": _empty_counts(),
        "usage": {key: dict(unknown) for key in MEASURES},
        "memory": {
            key: {"value": None, "provenance": NOT_APPLICABLE}
            for key in ("stale_leaks", "storage_bytes", "projection_chars", "latency_ms")
        },
        "safety_failures": [],
        "stopped_reason": reason,
        "journal": "absent",
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
    for key in MEASURES:
        measure = record.get("usage", {}).get(key)
        if not isinstance(measure, dict) or measure.get("provenance") not in PROVENANCES:
            problems.append(f"usage.{key} has no known provenance")
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
) -> OracleRun:
    """Run the hidden tests against ``commit``, as ``validation.commands`` run.

    An exact export with the hidden files laid over it, so what the candidate
    did to those paths does not matter; the allow-listed environment and the
    credential preflight; the export-command sandbox under ``required``. What
    could not be launched or was refused is ``not_run``: only exit 0 is a pass.
    """
    names = cfg.execution.environment_names()
    try:
        with export_commit_tree(runner, repo, commit) as exported:
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
    except AutoForgeError as exc:
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
    return {
        "base": at_base.status,
        "reference": at_reference.status,
        "coupled_to_new_names": bool(_COUPLED.search(at_base.stdout)),
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
    options = ["--tb=no", "-rfE", *(("-n", str(workers)) if workers else ())]
    command = [*validation_command(manifest, task), *options]
    run = run_oracle(repo, task["base_sha"], {}, command, cfg)
    return {"status": run.status, "failing_modules": failing_modules(run.stdout)}


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
        return float(record["outcome"] == VERIFIED), observed
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
            provenance[source] += 1
            totals[side][0] += cost or 0.0
            totals[side][1] += measure_of(record, "verified_success")[0] or 0.0
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


def _hard_gate(
    gate: str,
    treated: Sequence[Mapping],
    treatment: str,
    harness: Mapping | None,
    prereg: Mapping,
) -> dict:
    if gate == "safety_failures":
        found = sum(len(record["safety_failures"]) for record in treated)
        return {"gate": gate, "verdict": "fail" if found else "pass", "count": found}
    if gate == "stale_memory_leaks":
        values = [record["memory"]["stale_leaks"] for record in treated]
        if all(v["provenance"] == NOT_APPLICABLE for v in values) or any(
            v["provenance"] != MEASURED for v in values
        ):
            return {"gate": gate, "verdict": "unknown", "why": "not measured on this arm"}
        found = sum(v["value"] for v in values)
        return {"gate": gate, "verdict": "fail" if found else "pass", "count": found}
    if gate == "memory_overhead":
        values = [
            (record["memory"][key], limit)
            for record in treated
            for key, limit in MEMORY_OVERHEAD.items()
        ]
        if any(measure["provenance"] != MEASURED for measure, _ in values) or not values:
            return {"gate": gate, "verdict": "unknown", "why": "not measured on this arm"}
        ceilings = prereg["ceilings"]["memory_overhead"]
        over = sum(measure["value"] > ceilings[limit] for measure, limit in values)
        return {"gate": gate, "verdict": "fail" if over else "pass", "count": over}
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
    """One contrast's decision, from real records only; see the protocol's section 4."""
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

    invalid = []
    if {r["manifest_digest"] for r in real} != {digest(manifest)}:
        invalid.append("a record was made under another manifest")
    if {r["preregistration_digest"] for r in real} != {digest(prereg)}:
        invalid.append("a record was made under another pre-registration")
    if any(base["settings_digest"] != treated["settings_digest"] for base, treated in pairs):
        invalid.append("a pair's settings are not equal")
    if any(r["counts"]["hidden_leaks"] for pair in pairs for r in pair):
        invalid.append("a hidden test reached an agent")
    if invalid:
        return {**result, "decision": NOT_EVALUATED, "why": "; ".join(invalid)}

    stats = prereg["statistics"]
    treated_runs = [pair[1] for pair in pairs]
    result["hard_gates"] = [
        _hard_gate(gate, treated_runs, treatment, harness, prereg)
        for gate in comparison["hard_gates"]
    ]
    result["primary"] = evaluate_primary(pairs, comparison["primary"], stats) if pairs else None
    result["non_regression"] = [
        evaluate_non_regression(pairs, criterion, stats)
        for criterion in comparison["non_regression"]
    ]
    result["per_task"] = _per_task_table(pairs)
    result["decision"], result["why"] = _decide(result, stats, len(expected))
    return result


def _per_task_table(pairs: Sequence[tuple[Mapping, Mapping]]) -> list[dict]:
    """Each task's seeds and successes on each side, so one outlier can be seen."""
    table: dict[str, dict] = {}
    for base, treated in pairs:
        row = table.setdefault(
            base["task"],
            {
                "task": base["task"],
                "memory_class": base["memory_class"],
                "seeds": 0,
                "baseline_verified": 0,
                "treatment_verified": 0,
            },
        )
        row["seeds"] += 1
        row["baseline_verified"] += base["outcome"] == VERIFIED
        row["treatment_verified"] += treated["outcome"] == VERIFIED
    return sorted(table.values(), key=lambda row: row["task"])


def _decide(result: Mapping, stats: Mapping, planned: int) -> tuple[str, str]:
    """NO_GO on evidence of harm; GO only with everything measured and within bounds."""
    if result["primary"] is None:
        return NOT_EVALUATED, "no pair of the plan has both runs"
    failed = [g["gate"] for g in result["hard_gates"] if g["verdict"] == "fail"]
    if failed:
        return NO_GO, f"hard gate failed: {', '.join(failed)}"
    regressed = [c["metric"] for c in result["non_regression"] if c["verdict"] == "fail"]
    if regressed:
        return NO_GO, f"past its non-regression margin: {', '.join(regressed)}"
    if result["primary"]["verdict"] == "fail":
        return NO_GO, "the interval lies below the smallest effect that matters"
    if result["missing"]:
        return INCONCLUSIVE, f"{len(result['missing'])} planned pair(s) have no record"
    if len(result["excluded"]) > stats["max_excluded_pair_share"] * planned:
        return INCONCLUSIVE, f"{len(result['excluded'])} pair(s) excluded, over the bound"
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
        "safety_failures": sum(len(r["safety_failures"]) for r in records),
        "incomplete_run_logs": sum(
            r["journal"] != "read" or r["counts"]["journal_bad_lines"] > 0
            for r in records
            if r["outcome"] not in STOPPED_BY
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
    for name, contrasts in quality["comparisons"].items():
        for contrast in contrasts:
            lines.append(
                f"  {name}: {contrast['treatment']} vs {contrast['baseline']}: "
                f"{contrast['decision']} ({contrast['why']})"
            )
    lines += ["", "NOT EVIDENCE FOR AUTOFORGE"]
    lines += [f"  {item}" for item in report["not_evidence_for_autoforge"]]
    return "\n".join(lines)


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
        f"{uncertainty['measured_cost_usd']}; estimated cost {uncertainty['estimated_cost_usd']}",
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
            if result != {"base": "fail", "reference": "pass", "coupled_to_new_names": False}
        ]
    if args.base_suite:
        suites = {
            task["id"]: verify_base_suite(repo, manifest, task, cfg, args.workers)
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
    if args.stopped_by:
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
        cfg = load_config_file(args.config)
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
        if problems:
            for problem in problems:
                print(f"collect refuses: {problem}")
            return 2
        overlay = hidden_overlay(args.oracle_repo, task)
        command = oracle_command(manifest, task)
        denied = (str(state_dir), repo, str(Path(args.oracle_repo).resolve()))
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
