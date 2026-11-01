"""How long each CI run of the suite took, and how that compares with main (#344).

No test: the tool behind the duration report of the ``test`` job of
``.github/workflows/ci.yml``, and what a maintainer runs by hand on a JUnit
report or on the artifacts CI uploaded. Standard library only, so it runs on
any interpreter without the project installed (``python -I``).

``report`` is the job's step after ``pytest``::

    python -I tests/ci_durations.py report --dir "$RUNNER_TEMP/durations" \\
        --python 3.11 --cache-hit true --pytest-outcome success --check-run "test (py3.11)"

It reads ``DIR/junit.xml`` (pytest's ``--junitxml``) and ``DIR/history/history.json``
(main's recent runs, restored from the Actions cache when there is one), and writes

- ``DIR/report/summary.json``: the run (commit, ref, event, run, attempt and
  its URL, the check run's name, the pytest step's outcome), its category
  (job, interpreter, test selection, uv cache state, runner), the counts
  (tests, passed, failed, errors, skipped, xfailed), the suite's wall time
  and the tests' own, each module's time, the slowest tests and the skips by
  reason;
- ``DIR/report/tests.json``: every test's id, outcome and time;
- ``DIR/report/comparison.json`` and ``DIR/report/summary.md``: the verdict
  against main's recent runs of the same category, the Markdown also
  appended to ``$GITHUB_STEP_SUMMARY``;
- on a push to main, ``DIR/history/history.json`` again, with this run added
  when it is clean.

**Report only.** ``report`` exits 0 whatever it finds, and the workflow runs
it with ``continue-on-error``: the job's result is pytest's, and the required
``ci`` check means what it meant. A missing or unreadable JUnit report, a run
with failures and a category without enough history each get a verdict that
says so (``no-data``, ``not-comparable``, ``insufficient-baseline``), never
``ok``. Nothing it writes holds a failure's message or anything a test
printed: only test ids, outcomes, times and skip reasons, which pytest's
``-rs`` prints in the job's log anyway.

**The policy** (docs/development.md, "Test durations in CI"): a run is
compared with the last :data:`WINDOW` clean runs of a push to main in its
category, recorded within :data:`MAX_AGE_DAYS` before it, one per commit. With
fewer than :data:`MIN_SAMPLES` there is no verdict. Above :data:`REGRESSION`
times the window's p95 (nearest rank) it is a ``regression``, above
:data:`SLOWER` times its median ``slower``, below :data:`FASTER` times its
median ``faster``, and ``ok`` otherwise.

By hand, on a local run or on artifacts downloaded with ``gh run download``::

    uv run pytest --junitxml=/tmp/d/junit.xml
    python -I tests/ci_durations.py summarize --junit /tmp/d/junit.xml --out /tmp/d/report
    python -I tests/ci_durations.py compare --current /tmp/d/report/summary.json \\
        --baseline ARTIFACTS
"""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = "autoforge-ci-durations/1"
# The Actions cache key of the history names this version (`ci-durations-v1-`);
# tests/test_ci_workflow.py keeps the two equal, so a new schema starts a new history.
HISTORY_SCHEMA = "autoforge-ci-durations-history/1"

# The baseline policy (docs/development.md, "Test durations in CI").
WINDOW = 20
MIN_SAMPLES = 5
MAX_AGE_DAYS = 30
SLOWER = 1.10  # times the window's median: #341's "10% above the median"
REGRESSION = 1.10  # times the window's p95: beyond the spread the window itself shows
FASTER = 0.90  # times the window's median
BASELINE_EVENT = "push"
BASELINE_REF = "refs/heads/main"
# How long the workflow keeps a run's report (`retention-days`), as long as the
# oldest sample a window may hold; tests/test_ci_workflow.py keeps them equal.
ARTIFACT_RETENTION_DAYS = MAX_AGE_DAYS
# Samples kept per category in the history file, beyond the window, so a window
# still fills after runs that were not clean.
HISTORY_CAP = 60

# Where `report --dir` reads and writes.
JUNIT_NAME = "junit.xml"
REPORT_DIR = "report"
HISTORY_FILE = "history/history.json"

SLOWEST = 20
MODULE_CHANGES = 5
TREND_ROWS = 10
REASON_LIMIT = 300

OUTCOMES = ("passed", "failed", "errors", "skipped", "xfailed")
# A test reported twice (an error in teardown after its call) keeps the worse outcome.
_RANK = {"passed": 0, "xfailed": 1, "skipped": 2, "failed": 3, "errors": 4}
CATEGORY_KEYS = ("job", "python", "selection", "cache", "runner")
RUN_KEYS = (
    "sha",
    "ref",
    "event",
    "run_id",
    "run_attempt",
    "run_url",
    "check_run",
    "pytest_outcome",
    "image",
    "recorded_at",
)
CACHE_STATES = {"true": "warm", "false": "cold"}


@dataclass(frozen=True)
class Case:
    """One test of a JUnit report."""

    id: str
    module: str
    outcome: str
    seconds: float
    reason: str = ""


def _seconds(value: str | None) -> float:
    seconds = float(value or 0)
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError(f"a time of {value!r} seconds")
    return seconds


def _outcome(case: ET.Element) -> tuple[str, str]:
    """A test case's outcome, and its skip reason; never a failure's message."""
    outcome, reason = "passed", ""
    for child in case:
        if child.tag == "skipped":
            found = "xfailed" if child.get("type") == "pytest.xfail" else "skipped"
            text = child.get("message") or ""
        elif child.tag == "failure":
            found, text = "failed", ""
        elif child.tag == "error":
            found, text = "errors", ""
        else:
            continue
        if _RANK[found] > _RANK[outcome]:
            outcome, reason = found, text
    return outcome, reason


def module_of(classname: str) -> str:
    """``tests.test_engine`` for ``tests.test_engine`` and ``tests.test_engine.TestX``."""
    parts = classname.split(".")
    return ".".join(parts[:2]) if parts[:1] == ["tests"] else parts[0]


def parse_junit(xml_text: str) -> tuple[list[Case], float | None]:
    """Every test of a JUnit XML report by id (``classname::name``), and the suite's wall time.

    The wall time is the ``time`` of the report's test suites (pytest's whole
    session), ``None`` when one has none. Raises ``ValueError`` (or
    ``ET.ParseError``) for anything that is not such a report.
    """
    root = ET.fromstring(xml_text)
    if root.tag == "testsuite":
        suites = [root]
    elif root.tag == "testsuites":
        suites = list(root.iter("testsuite"))
    else:
        raise ValueError(f"not a JUnit report: its root element is <{root.tag}>")
    if not suites:
        raise ValueError("not a JUnit report: it holds no <testsuite>")
    times = [suite.get("time") for suite in suites]
    suite_seconds = sum(_seconds(t) for t in times) if all(times) else None
    cases: dict[str, Case] = {}
    for element in root.iter("testcase"):
        classname = element.get("classname", "")
        test_id = f"{classname}::{element.get('name', '')}"
        outcome, reason = _outcome(element)
        seconds = _seconds(element.get("time"))
        earlier = cases.get(test_id)
        if earlier is not None:
            if _RANK[earlier.outcome] >= _RANK[outcome]:
                outcome, reason = earlier.outcome, earlier.reason
            seconds = max(seconds, earlier.seconds)
        cases[test_id] = Case(test_id, module_of(classname), outcome, seconds, reason)
    return list(cases.values()), suite_seconds


@dataclass(frozen=True)
class Context:
    """What a run is, from the step's options and the variables GitHub Actions sets."""

    job: str
    python: str
    selection: str
    cache: str
    runner: str
    sha: str
    ref: str
    event: str
    run_id: str
    run_attempt: str
    run_url: str
    check_run: str
    pytest_outcome: str
    image: str
    recorded_at: str

    def category(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in CATEGORY_KEYS}

    def run(self) -> dict[str, str]:
        return {key: getattr(self, key) for key in RUN_KEYS}


def cache_state(cache_hit: str) -> str:
    """setup-uv's ``cache-hit`` output as ``warm`` or ``cold``; ``unknown`` for anything else."""
    return CACHE_STATES.get(cache_hit.strip().lower(), "unknown")


def context_from(
    environ: Mapping[str, str],
    now: datetime,
    *,
    python: str = "",
    selection: str = "default",
    cache_hit: str = "",
    pytest_outcome: str = "",
    check_run: str = "",
) -> Context:
    """The run's context. Outside GitHub Actions the run fields are empty and the runner local."""
    if environ.get("GITHUB_ACTIONS") == "true":
        runner_kind = environ.get("RUNNER_ENVIRONMENT") or "github-hosted"
        parts = (runner_kind, environ.get("RUNNER_OS", ""), environ.get("RUNNER_ARCH", ""))
        runner = " ".join(part for part in parts if part)
    else:
        runner = f"local {platform.system()} {platform.machine()}"
    server = environ.get("GITHUB_SERVER_URL", "").rstrip("/")
    repository = environ.get("GITHUB_REPOSITORY", "")
    run_id = environ.get("GITHUB_RUN_ID", "")
    attempt = environ.get("GITHUB_RUN_ATTEMPT", "")
    run_url = ""
    if server and repository and run_id:
        run_url = f"{server}/{repository}/actions/runs/{run_id}"
        if attempt:
            run_url += f"/attempts/{attempt}"
    image = " ".join(v for v in (environ.get("ImageOS", ""), environ.get("ImageVersion", "")) if v)
    return Context(
        job=environ.get("GITHUB_JOB", "") or "local",
        python=python or f"{sys.version_info.major}.{sys.version_info.minor}",
        selection=selection,
        cache=cache_state(cache_hit),
        runner=runner,
        sha=environ.get("GITHUB_SHA", ""),
        ref=environ.get("GITHUB_REF", ""),
        event=environ.get("GITHUB_EVENT_NAME", ""),
        run_id=run_id,
        run_attempt=attempt,
        run_url=run_url,
        check_run=check_run,
        pytest_outcome=pytest_outcome,
        image=image,
        recorded_at=now.astimezone(UTC).isoformat(timespec="seconds"),
    )


def _reason(text: str) -> str:
    one_line = " ".join(text.split())
    if len(one_line) <= REASON_LIMIT:
        return one_line
    return one_line[: REASON_LIMIT - 1] + "…"


def summarize(junit: Path, ctx: Context) -> tuple[dict, list[dict]]:
    """The summary of one run and its tests, from its JUnit report at ``junit``.

    A report that is missing or does not read gives a summary whose
    ``status`` says so, with no counts and ``clean`` false. A run is clean
    when its report holds at least one test and no failure or error, and the
    pytest step succeeded (when its outcome is given).
    """
    summary: dict = {
        "schema": SCHEMA,
        "category": ctx.category(),
        "run": ctx.run(),
        "status": "complete",
        "problem": "",
        "clean": False,
        "counts": None,
        "suite_seconds": None,
        "test_seconds": None,
        "modules": {},
        "slowest": [],
        "skip_reasons": [],
    }
    step = f"the pytest step's outcome was {ctx.pytest_outcome or 'not given'}"
    try:
        text = junit.read_text(encoding="utf-8")
    except FileNotFoundError:
        summary.update(status="missing", problem=f"no JUnit report at {junit}; {step}")
        return summary, []
    except (OSError, UnicodeDecodeError) as exc:
        problem = f"the JUnit report at {junit} cannot be read ({exc}); {step}"
        summary.update(status="unreadable", problem=problem)
        return summary, []
    try:
        cases, suite_seconds = parse_junit(text)
    except (ET.ParseError, ValueError) as exc:
        problem = f"the JUnit report at {junit} does not parse ({exc}); {step}"
        summary.update(status="unreadable", problem=problem)
        return summary, []
    counts = dict.fromkeys(OUTCOMES, 0)
    modules: dict[str, float] = defaultdict(float)
    for case in cases:
        counts[case.outcome] += 1
        modules[case.module] += case.seconds
    counts["tests"] = len(cases)
    test_seconds = sum(case.seconds for case in cases)
    reasons = Counter(
        _reason(case.reason) for case in cases if case.outcome in ("skipped", "xfailed")
    )
    problems = []
    if not cases:
        problems.append("the report holds no test")
    if counts["failed"] or counts["errors"]:
        problems.append(f"{counts['failed']} failed and {counts['errors']} errors")
    if ctx.pytest_outcome and ctx.pytest_outcome != "success":
        problems.append(step)
    summary.update(
        problem="; ".join(problems),
        clean=not problems,
        counts=counts,
        suite_seconds=round(test_seconds if suite_seconds is None else suite_seconds, 3),
        test_seconds=round(test_seconds, 3),
        modules={name: round(modules[name], 3) for name in sorted(modules)},
        slowest=[
            {"id": case.id, "seconds": round(case.seconds, 3)}
            for case in sorted(cases, key=lambda c: (-c.seconds, c.id))[:SLOWEST]
        ],
        skip_reasons=[
            {"reason": reason, "count": count}
            for reason, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
        ],
    )
    tests = [
        {"id": case.id, "outcome": case.outcome, "seconds": round(case.seconds, 3)}
        for case in sorted(cases, key=lambda c: c.id)
    ]
    return summary, tests


# -- samples, history and the baseline window ---------------------------------


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _number(value: object) -> bool:
    return (
        isinstance(value, int | float)
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def valid_sample(sample: object) -> bool:
    """Whether ``sample`` is a summary this tool wrote, field by field; nothing else is read."""
    if not isinstance(sample, dict) or sample.get("schema") != SCHEMA:
        return False
    category, run = sample.get("category"), sample.get("run")
    if not isinstance(category, dict) or not isinstance(run, dict):
        return False
    if any(not isinstance(category.get(key), str) for key in CATEGORY_KEYS):
        return False
    if any(not isinstance(run.get(key), str) for key in RUN_KEYS):
        return False
    if _timestamp(run["recorded_at"]) is None or not isinstance(sample.get("clean"), bool):
        return False
    if sample.get("status") != "complete":
        return sample.get("status") in ("missing", "unreadable") and not sample["clean"]
    counts, modules = sample.get("counts"), sample.get("modules")
    if not isinstance(counts, dict) or not isinstance(modules, dict):
        return False
    if any(not isinstance(counts.get(key), int) for key in (*OUTCOMES, "tests")):
        return False
    if not _number(sample.get("suite_seconds")) or not _number(sample.get("test_seconds")):
        return False
    return all(isinstance(name, str) and _number(value) for name, value in modules.items())


def is_baseline(sample: dict) -> bool:
    """A clean, complete run of a push to main: the only kind a window holds."""
    run = sample["run"]
    return (
        sample["status"] == "complete"
        and sample["clean"]
        and run["event"] == BASELINE_EVENT
        and run["ref"] == BASELINE_REF
    )


def history_sample(summary: dict) -> dict:
    """What the history keeps of a summary: all but its slowest tests and skip reasons."""
    return {k: v for k, v in summary.items() if k not in ("slowest", "skip_reasons")}


def _recorded(sample: dict) -> datetime:
    moment = _timestamp(sample["run"]["recorded_at"])
    assert moment is not None, "only valid samples are ordered"
    return moment


def _key(category: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(category[key] for key in CATEGORY_KEYS)


@dataclass(frozen=True)
class Policy:
    window: int = WINDOW
    min_samples: int = MIN_SAMPLES
    max_age_days: int = MAX_AGE_DAYS


def window_of(samples: Iterable[dict], current: dict, policy: Policy) -> list[dict]:
    """The baseline of ``current``: newest first, at most ``policy.window`` samples.

    Only clean runs of a push to main in the same category, recorded within
    ``policy.max_age_days`` before ``current`` (never after it, so an older
    run compared again gets the same window), and neither ``current`` itself
    nor another run of its commit. One sample per commit: the newest.
    """
    newest = _recorded(current)
    oldest = newest - timedelta(days=policy.max_age_days)
    run = current["run"]
    category = _key(current["category"])
    by_commit: dict[str, dict] = {}
    for sample in samples:
        if not valid_sample(sample) or not is_baseline(sample):
            continue
        if _key(sample["category"]) != category:
            continue
        other = sample["run"]
        if run["run_id"] and other["run_id"] == run["run_id"]:
            continue
        if run["sha"] and other["sha"] == run["sha"]:
            continue
        if not oldest <= _recorded(sample) <= newest:
            continue
        kept = by_commit.get(other["sha"])
        if kept is None or _recorded(sample) > _recorded(kept):
            by_commit[other["sha"]] = sample
    ordered = sorted(by_commit.values(), key=_recorded, reverse=True)
    return ordered[: policy.window]


def p95(values: Sequence[float]) -> float:
    """The nearest-rank 95th percentile, as tests/parallel_bench.py takes it."""
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


def record(samples: list[dict], summary: dict, cap: int = HISTORY_CAP) -> tuple[list[dict], str]:
    """The history with ``summary`` added when it is a baseline run, and what was done.

    An earlier sample of the same commit and category is replaced (a re-run
    attempt), and each category keeps its ``cap`` newest samples. Samples
    this tool did not write are dropped.
    """
    kept = [sample for sample in samples if valid_sample(sample)]
    note = ""
    if not valid_sample(summary) or not is_baseline(summary):
        why = summary.get("problem") or "it is not a run of a push to main"
        note = f"this run was not added to main's history: {why}"
    else:
        sample = history_sample(summary)
        key, sha = _key(sample["category"]), sample["run"]["sha"]
        kept = [s for s in kept if not (_key(s["category"]) == key and s["run"]["sha"] == sha)]
        kept.append(sample)
        note = "this run was added to main's history"
    grouped: dict[tuple[str, ...], list[dict]] = defaultdict(list)
    for sample in kept:
        grouped[_key(sample["category"])].append(sample)
    trimmed = [
        sample
        for group in grouped.values()
        for sample in sorted(group, key=_recorded, reverse=True)[:cap]
    ]
    return sorted(trimmed, key=_recorded), note


def load_history(path: Path) -> tuple[list[dict], str]:
    """The samples of a history file, and a note when it is missing or not all read."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [], "no history of main was restored (none saved yet, or the cache expired)"
    except (OSError, ValueError) as exc:
        return [], f"the history at {path} was ignored: it does not read ({exc})"
    if not isinstance(data, dict) or data.get("schema") != HISTORY_SCHEMA:
        return [], f"the history at {path} was ignored: it is not a {HISTORY_SCHEMA} file"
    samples = data.get("samples")
    if not isinstance(samples, list):
        return [], f"the history at {path} was ignored: it holds no list of samples"
    valid = [sample for sample in samples if valid_sample(sample)]
    note = ""
    if len(valid) != len(samples):
        note = f"{len(samples) - len(valid)} malformed samples of the history were ignored"
    return valid, note


def history_text(samples: list[dict]) -> str:
    return json.dumps({"schema": HISTORY_SCHEMA, "samples": samples}, indent=1) + "\n"


def baseline_samples(paths: Iterable[Path]) -> tuple[list[dict], list[str]]:
    """Samples from summaries and history files, or the directories that hold them."""
    samples: list[dict] = []
    notes: list[str] = []
    files: list[Path] = []
    for path in paths:
        if path.is_dir():
            files.extend(sorted(path.rglob("summary.json")))
            files.extend(sorted(path.rglob("history.json")))
        else:
            files.append(path)
    for file in files:
        try:
            data = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            notes.append(f"{file} was ignored: it does not read ({exc})")
            continue
        if isinstance(data, dict) and data.get("schema") == HISTORY_SCHEMA:
            found, note = load_history(file)
            samples.extend(found)
            if note:
                notes.append(f"{file}: {note}")
        elif valid_sample(data):
            samples.append(data)
        else:
            notes.append(f"{file} was ignored: it is neither a summary nor a history")
    return samples, notes


# -- the comparison ------------------------------------------------------------


def compare(current: dict, samples: list[dict], policy: Policy | None = None) -> dict:
    """The verdict on ``current`` against its baseline window, with what explains it."""
    policy = policy or Policy()
    window = window_of(samples, current, policy) if valid_sample(current) else []
    seconds = [sample["suite_seconds"] for sample in window]
    result: dict = {
        "schema": SCHEMA,
        "category": current["category"],
        "run": current["run"],
        "verdict": "",
        "reason": "",
        "current_seconds": current.get("suite_seconds"),
        "samples": len(window),
        "median": None,
        "p95": None,
        "ratio": None,
        "thresholds": None,
        "policy": asdict(policy),
        "notes": _count_notes(current, window),
        "modules": [],
        "other_cache": _other_cache(current, samples, policy),
        "window": [_trend_row(sample) for sample in window[:TREND_ROWS]],
    }
    if current["status"] != "complete":
        result.update(verdict="no-data", reason=current["problem"])
        return result
    if not current["clean"]:
        result.update(verdict="not-comparable", reason=current["problem"])
        return result
    if len(window) < policy.min_samples:
        reason = (
            f"{len(window)} clean runs of a push to main in this category within "
            f"{policy.max_age_days} days, and a verdict needs {policy.min_samples}"
        )
        result.update(verdict="insufficient-baseline", reason=reason)
        return result
    median, high, now = statistics.median(seconds), p95(seconds), current["suite_seconds"]
    thresholds = {
        "regression": round(high * REGRESSION, 3),
        "slower": round(median * SLOWER, 3),
        "faster": round(median * FASTER, 3),
    }
    if now > thresholds["regression"]:
        verdict = "regression"
    elif now > thresholds["slower"]:
        verdict = "slower"
    elif now < thresholds["faster"]:
        verdict = "faster"
    else:
        verdict = "ok"
    reason = (
        f"{now:.1f} s against a median of {median:.1f} s and a p95 of {high:.1f} s over "
        f"the last {len(window)} clean runs of a push to main"
    )
    result.update(
        verdict=verdict,
        reason=reason,
        median=round(median, 3),
        p95=round(high, 3),
        ratio=round(now / median, 3) if median else None,
        thresholds=thresholds,
        modules=_module_changes(current, window),
    )
    return result


def _count_notes(current: dict, window: list[dict]) -> list[str]:
    """What changed in what ran, against the newest run of the window."""
    if not window or current["status"] != "complete":
        return []
    latest = window[0]
    notes = []
    sha = latest["run"]["sha"][:12] or "?"
    tests = current["counts"]["tests"] - latest["counts"]["tests"]
    skipped = current["counts"]["skipped"] - latest["counts"]["skipped"]
    if tests < 0:
        notes.append(
            f"{-tests} fewer tests than main's run at {sha}: a run that tests less is "
            "faster without being an improvement"
        )
    elif tests > 0:
        notes.append(f"{tests} more tests than main's run at {sha}")
    if skipped > 0:
        notes.append(f"{skipped} more skipped tests than main's run at {sha}")
    elif skipped < 0:
        notes.append(f"{-skipped} fewer skipped tests than main's run at {sha}")
    image, latest_image = current["run"]["image"], latest["run"]["image"]
    if image and latest_image and image != latest_image:
        notes.append(f"the runner image is {image}; main's run at {sha} had {latest_image}")
    return notes


def _module_changes(current: dict, window: list[dict]) -> list[dict]:
    """The modules whose time grew most against their median over the window."""
    changes = []
    for name, now in current["modules"].items():
        before = [sample["modules"][name] for sample in window if name in sample["modules"]]
        if not before:
            continue
        median = statistics.median(before)
        if now > median:
            changes.append(
                {"module": name, "seconds": now, "median": round(median, 3), "delta": now - median}
            )
    changes.sort(key=lambda change: (-change["delta"], change["module"]))
    return [{**change, "delta": round(change["delta"], 3)} for change in changes[:MODULE_CHANGES]]


def _other_cache(current: dict, samples: list[dict], policy: Policy) -> list[dict]:
    """The window of the same category in each other uv cache state, for reference only."""
    if not valid_sample(current):
        return []
    rows = []
    for state in ("warm", "cold", "unknown"):
        if state == current["category"]["cache"]:
            continue
        other = {**current, "category": {**current["category"], "cache": state}}
        window = window_of(samples, other, policy)
        if window:
            seconds = [sample["suite_seconds"] for sample in window]
            rows.append(
                {
                    "cache": state,
                    "samples": len(window),
                    "median": round(statistics.median(seconds), 3),
                }
            )
    return rows


def _trend_row(sample: dict) -> dict:
    run = sample["run"]
    return {
        "recorded_at": run["recorded_at"],
        "sha": run["sha"],
        "run_url": run["run_url"],
        "suite_seconds": sample["suite_seconds"],
        "tests": sample["counts"]["tests"],
        "skipped": sample["counts"]["skipped"],
    }


# -- Markdown ------------------------------------------------------------------


def _cell(text: object) -> str:
    return " ".join(str(text).split()).replace("|", "\\|")


def _link(text: str, url: str) -> str:
    return f"[{text}]({url})" if url else text


def render(summary: dict, comparison: dict | None) -> str:
    """The Markdown of a run and, when given, its comparison."""
    run, category = summary["run"], summary["category"]
    title = run["check_run"] or f"{category['job']} (py{category['python']})"
    lines = [
        f"### Test durations: {_cell(title)}",
        "",
        "Report only: this never decides the job or the required `ci` check "
        '(docs/development.md, "Test durations in CI").',
        "",
        "| | |",
        "|---|---|",
    ]
    if run["run_id"]:
        where = _link(f"run {run['run_id']}, attempt {run['run_attempt']}", run["run_url"])
        commit = f"`{run['sha'][:12]}`" if run["sha"] else "unknown"
        where += f", commit {commit}, {run['event'] or '?'} to {run['ref'] or '?'}"
    else:
        where = "a local run, not on GitHub Actions"
    lines.append(f"| run | {where} |")
    if run["check_run"]:
        lines.append(
            f"| check | `{_cell(run['check_run'])}`, aggregated by the required check `ci` |"
        )
    lines.append(f"| pytest step | {run['pytest_outcome'] or 'not given'} |")
    image = f" (image {_cell(run['image'])})" if run["image"] else ""
    lines.append(
        f"| category | Python {_cell(category['python'])}, {_cell(category['selection'])} "
        f"selection, uv cache {category['cache']}, {_cell(category['runner'])}{image} |"
    )
    counts = summary["counts"]
    if counts is None:
        lines.append(f"| report | **{summary['status']}**: {_cell(summary['problem'])} |")
    else:
        lines.append(
            f"| tests | {counts['tests']}: {counts['passed']} passed, {counts['failed']} "
            f"failed, {counts['errors']} errors, {counts['skipped']} skipped, "
            f"{counts['xfailed']} xfailed |"
        )
        lines.append(
            f"| suite | {summary['suite_seconds']:.1f} s wall (the tests' own time "
            f"{summary['test_seconds']:.1f} s) |"
        )
        if summary["problem"]:
            lines.append(f"| not clean | {_cell(summary['problem'])} |")
    if comparison is not None:
        lines += ["", *_render_comparison(comparison)]
    if summary["slowest"]:
        lines += ["", "<details><summary>Slowest tests</summary>", "", "| test | s |", "|---|---|"]
        lines += [f"| {_cell(t['id'])} | {t['seconds']:.2f} |" for t in summary["slowest"][:10]]
        lines += ["", "</details>"]
    if summary["skip_reasons"]:
        lines += ["", "<details><summary>Skips by reason</summary>", "", "| n | reason |"]
        lines += ["|---|---|"]
        lines += [f"| {r['count']} | {_cell(r['reason'])} |" for r in summary["skip_reasons"]]
        lines += ["", "</details>"]
    return "\n".join(lines) + "\n"


def _render_comparison(comparison: dict) -> list[str]:
    lines = [f"**Verdict: {comparison['verdict']}**: {_cell(comparison['reason'])}."]
    thresholds = comparison["thresholds"]
    if thresholds:
        lines += [
            "",
            f"Ratio to the median {comparison['ratio']:.2f}x. Slower above "
            f"{thresholds['slower']:.1f} s ({SLOWER:.2f}x the median), regression above "
            f"{thresholds['regression']:.1f} s ({REGRESSION:.2f}x the p95), faster below "
            f"{thresholds['faster']:.1f} s ({FASTER:.2f}x the median).",
        ]
    if comparison["notes"]:
        lines += ["", *(f"- {_cell(note)}" for note in comparison["notes"])]
    for row in comparison["other_cache"]:
        lines += [
            "",
            f"For reference, not a verdict: {row['samples']} runs of main with a "
            f"{row['cache']} uv cache, median {row['median']:.1f} s.",
        ]
    if comparison["modules"]:
        lines += ["", "| module grew most | s | median s | delta s |", "|---|---|---|---|"]
        lines += [
            f"| {_cell(m['module'])} | {m['seconds']:.1f} | {m['median']:.1f} | +{m['delta']:.1f} |"
            for m in comparison["modules"]
        ]
    if comparison["window"]:
        lines += [
            "",
            "<details><summary>Main's recent runs in this category</summary>",
            "",
            "| recorded | commit | s | tests | skipped |",
            "|---|---|---|---|---|",
        ]
        for row in comparison["window"]:
            commit = _link(f"`{row['sha'][:12]}`", row["run_url"])
            lines.append(
                f"| {row['recorded_at'][:16]} | {commit} | {row['suite_seconds']:.1f} | "
                f"{row['tests']} | {row['skipped']} |"
            )
        lines += ["", "</details>"]
    return lines


# -- the command line ----------------------------------------------------------


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=1, sort_keys=True) + "\n", encoding="utf-8")


def _write_tests(path: Path, tests: list[dict]) -> None:
    body = ",\n".join(json.dumps(test, sort_keys=True) for test in tests)
    path.write_text(f"[\n{body}\n]\n" if tests else "[]\n", encoding="utf-8")


def _context(args: argparse.Namespace, environ: Mapping[str, str], now: datetime) -> Context:
    return context_from(
        environ,
        now,
        python=args.python,
        selection=args.selection,
        cache_hit=args.cache_hit,
        pytest_outcome=args.pytest_outcome,
        check_run=args.check_run,
    )


def _write_report(out: Path, summary: dict, tests: list[dict], comparison: dict | None) -> str:
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "summary.json", summary)
    _write_tests(out / "tests.json", tests)
    if comparison is not None:
        _write_json(out / "comparison.json", comparison)
    text = render(summary, comparison)
    (out / "summary.md").write_text(text, encoding="utf-8")
    return text


def run_report(args: argparse.Namespace, environ: Mapping[str, str], now: datetime) -> int:
    """CI's step: summarize, compare with main's history, record a push to main; always 0."""
    directory = Path(args.dir)
    ctx = _context(args, environ, now)
    summary, tests = summarize(directory / JUNIT_NAME, ctx)
    history = directory / HISTORY_FILE
    samples, history_note = load_history(history)
    comparison = compare(summary, samples)
    notes = [history_note] if history_note else []
    if ctx.event == BASELINE_EVENT and ctx.ref == BASELINE_REF:
        updated, record_note = record(samples, summary)
        try:
            history.parent.mkdir(parents=True, exist_ok=True)
            history.write_text(history_text(updated), encoding="utf-8")
        except OSError as exc:
            record_note = f"main's history could not be written to {history}: {exc}"
        notes.append(record_note)
    comparison["notes"] = [*comparison["notes"], *notes]
    text = _write_report(directory / REPORT_DIR, summary, tests, comparison)
    print(text, end="")
    step_summary = environ.get("GITHUB_STEP_SUMMARY")
    if step_summary:
        with open(step_summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


def run_summarize(args: argparse.Namespace, environ: Mapping[str, str], now: datetime) -> int:
    summary, tests = summarize(Path(args.junit), _context(args, environ, now))
    print(_write_report(Path(args.out), summary, tests, None), end="")
    return 0 if summary["status"] == "complete" else 1


def run_compare(args: argparse.Namespace) -> int:
    try:
        current = json.loads(Path(args.current).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"{args.current} does not read: {exc}", file=sys.stderr)
        return 2
    if not valid_sample(current):
        print(f"{args.current} is not a summary this tool wrote ({SCHEMA})", file=sys.stderr)
        return 2
    samples, notes = baseline_samples(Path(path) for path in args.baseline)
    policy = Policy(args.window, args.min_samples, args.max_age_days)
    comparison = compare(current, samples, policy)
    comparison["notes"] = [*comparison["notes"], *notes]
    if args.json_out:
        _write_json(Path(args.json_out), comparison)
    print(render(current, comparison), end="")
    return 1 if args.fail_on_regression and comparison["verdict"] == "regression" else 0


def _positive(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError(f"{text} is not a positive integer")
    return value


def _add_context_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--python", default="", help="the interpreter, e.g. 3.11 (category)")
    parser.add_argument("--selection", default="default", help="the tests run (category)")
    parser.add_argument("--cache-hit", default="", help="setup-uv's cache-hit output")
    parser.add_argument("--pytest-outcome", default="", help="the pytest step's outcome")
    parser.add_argument("--check-run", default="", help="the check run's name on GitHub")


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="ci_durations", description=__doc__.split("\n\n")[0])
    commands = top.add_subparsers(dest="command", required=True)
    report = commands.add_parser("report", help="CI's step: summarize, compare and record")
    report.add_argument("--dir", required=True, help="holds junit.xml, report/ and history/")
    _add_context_options(report)
    summarize_ = commands.add_parser("summarize", help="the summary of one JUnit report")
    summarize_.add_argument("--junit", required=True)
    summarize_.add_argument("--out", required=True, help="the directory the report goes to")
    _add_context_options(summarize_)
    compare_ = commands.add_parser("compare", help="a summary against main's runs")
    compare_.add_argument("--current", required=True, help="a summary.json")
    compare_.add_argument(
        "--baseline",
        action="append",
        default=[],
        help="a summary.json, a history.json or a directory holding them; repeatable",
    )
    compare_.add_argument("--window", type=_positive, default=WINDOW)
    compare_.add_argument("--min-samples", type=_positive, default=MIN_SAMPLES)
    compare_.add_argument("--max-age-days", type=_positive, default=MAX_AGE_DAYS)
    compare_.add_argument("--json-out", default="", help="also write the comparison here")
    compare_.add_argument(
        "--fail-on-regression",
        action="store_true",
        help="exit 1 on a regression verdict (by hand only; CI never passes it)",
    )
    return top


def main(
    argv: Sequence[str] | None = None,
    environ: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> int:
    args = parser().parse_args(argv)
    env = os.environ if environ is None else environ
    moment = datetime.now(UTC) if now is None else now
    if args.command == "report":
        return run_report(args, env, moment)
    if args.command == "summarize":
        return run_summarize(args, env, moment)
    return run_compare(args)


if __name__ == "__main__":
    raise SystemExit(main())
