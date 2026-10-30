"""Serial and pytest-xdist runs of the whole suite, timed and reconciled (#340).

No test: the tool a maintainer runs by hand to decide whether, and how, the
suite may run on several workers. ``python -m tests.parallel_bench`` runs the
default suite (``pytest`` with the repository's own options) once per
configuration and repeat, and for each run records

- the wall time of the whole ``pytest`` process, collection included;
- the outcome of every test, read from the run's JUnit XML;
- the counts of pytest's summary line (passed, skipped, deselected, ...).

It then reconciles every run against the first serial one that passed: the
same test ids with the same outcomes, so a test that a configuration loses,
adds, skips or fails shows up by name rather than as a changed total. A run
that differs, or fails, is reported and never timed: the report gives each
configuration's median and p95 (nearest rank: with fewer than 20 repeats
that is the slowest run) over its clean runs only, and none for a
configuration without one; without a passing serial run nothing is
reconciled and nothing is timed. It also gives the host the numbers came
from: commit, interpreter, pytest and xdist versions, the machine (a keyed
hash of its machine ID and host name), CPU model, CPUs, memory, kernel, git
and the load average around every run.

Runs are interleaved (every configuration once, then every configuration
again), so a change in the host's load over the session spreads across all
of them instead of landing on one. A configuration given with ``--once``
runs in the first round only: a screening run, reconciled like the others.
A ``--collect-only`` run first compiles the bytecode, so the first timed run
is not the only one that pays for it.

Configurations are ``serial`` or ``n<workers>-<dist>``, ``<dist>`` one of
xdist's ``load``, ``loadfile``, ``loadscope``, ``loadgroup`` and ``worksteal``::

    uv run python -m tests.parallel_bench --repeats 3 \\
        --config serial --config n2-load --config n4-load --once n4-loadfile \\
        --out /tmp/bench

It measures a commit: it refuses a worktree with anything changed, staged or
untracked (``git status``), or one git fails to read, before the first run
and after every run, and an ``--out`` inside the worktree.

Every run loads this module as a pytest plugin (``-p tests.parallel_bench``),
which records how pytest-xdist actually ran it: on how many workers, with
which distribution. Whatever else sets xdist's options (``PYTEST_ADDOPTS``,
the pytest arguments after ``--``, the repository's ``addopts``) applies to
every configuration, so a run that did not run as its configuration names
stops the benchmark, unrecorded: a ``serial`` run on workers is no reference.

Nothing is written but ``--out``: ``provenance.json``, a JUnit XML file, a
log and a record per run, and ``results.json``; the Markdown report goes to
stdout. The same command again, on the same ``--out``, keeps every run that
has its record and makes only the others, so a benchmark of several hours
survives an interruption; it refuses an ``--out`` measured on another commit,
interpreter, pytest, xdist, host (the machine, its CPU model, CPUs, memory,
kernel, git), set of CPUs this process may run on, temporary directory, or
pytest arguments. A run is recorded only once its JUnit report reads: one
that left none (pytest could not start, or was killed) is reported as
failed, not reconciled or timed, and runs again on resume.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import platform
import re
import statistics
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
# The module each run loads as a plugin, and its option naming where the run's mode goes.
PLUGIN = "tests.parallel_bench"
MODE_OPTION = "--parallel-bench-mode"
SERIAL = "serial"
DISTRIBUTIONS = ("load", "loadfile", "loadscope", "loadgroup", "worksteal")
_CONFIG = re.compile(r"n(?P<workers>[1-9][0-9]*)-(?P<dist>[a-z]+)")
# pytest's last summary line: "11550 passed, 6 skipped, 1 deselected in 612.34s (0:10:12)".
_SUMMARY = re.compile(r"^=* ?(?P<counts>\d+ [a-z]+(?:, \d+ [a-z]+)*) in [0-9.]+s\b")
_PLURALS = {"errors": "error", "warnings": "warning", "reruns": "rerun"}
# --collect-only's: "11579/11580 tests collected (1 deselected) in 2.76s".
_COLLECTED = re.compile(
    r"^(?P<selected>\d+)(?:/(?P<collected>\d+))? tests? collected"
    r"(?: \((?P<deselected>\d+) deselected\))? in [0-9.]+s\b"
)


@dataclass(frozen=True)
class Config:
    """One way of running the suite: ``workers == 0`` is the serial run."""

    name: str
    workers: int = 0
    dist: str = ""

    def pytest_args(self) -> list[str]:
        return [] if not self.workers else ["-n", str(self.workers), f"--dist={self.dist}"]

    def mode(self) -> dict[str, object]:
        """How a run of this configuration is distributed, as the plugin records it."""
        return {"workers": self.workers, "dist": self.dist or "no"}


def parse_config(name: str) -> Config:
    """``serial`` or ``n<workers>-<dist>``; anything else is refused."""
    if name == SERIAL:
        return Config(SERIAL)
    match = _CONFIG.fullmatch(name)
    if not match or match["dist"] not in DISTRIBUTIONS:
        raise ValueError(
            f"unknown configuration {name!r}: expected {SERIAL!r} or n<workers>-<dist>, "
            f"<dist> one of {', '.join(DISTRIBUTIONS)}"
        )
    return Config(name, int(match["workers"]), match["dist"])


# The plugin. pytest-xdist calls pytest_xdist_setupnodes in the controlling
# process, once, with a spec per worker, and only when it distributes the run.
# That process writes the mode as the session ends; a worker never does.
_WORKERS = pytest.StashKey[int]()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        MODE_OPTION, metavar="PATH", help="tests/parallel_bench.py: record the run's xdist mode"
    )


@pytest.hookimpl(optionalhook=True)
def pytest_xdist_setupnodes(config: pytest.Config, specs: Sequence[object]) -> None:
    config.stash[_WORKERS] = len(specs)


def pytest_sessionfinish(session: pytest.Session) -> None:
    config = session.config
    path = config.getoption(MODE_OPTION)
    if not path or hasattr(config, "workerinput"):
        return
    workers = config.stash.get(_WORKERS, 0)
    mode = {"workers": workers, "dist": config.getoption("dist", "no") if workers else "no"}
    Path(path).write_text(json.dumps(mode) + "\n", encoding="utf-8")


def describe_mode(mode: dict[str, object]) -> str:
    """How a run ran, for a message: ``serially``, ``on 4 workers (--dist=load)``."""
    if not mode.get("workers"):
        return "serially"
    return f"on {mode['workers']} workers (--dist={mode.get('dist')})"


def junit_outcomes(xml_text: str) -> dict[str, str]:
    """Every test of a JUnit XML report by id (``classname::name``), with its outcome.

    The outcome is ``passed``, ``skipped``, ``failure`` or ``error``. A test
    reported twice (pytest adds a second entry for an error in teardown)
    keeps the worse outcome.
    """
    rank = {"passed": 0, "skipped": 1, "failure": 2, "error": 3}
    outcomes: dict[str, str] = {}
    for case in ET.fromstring(xml_text).iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        outcome = "passed"
        for child in case:
            if child.tag in rank and rank[child.tag] > rank[outcome]:
                outcome = child.tag
        if rank[outcome] >= rank[outcomes.get(test_id, "passed")]:
            outcomes[test_id] = outcome
    return outcomes


def junit_file_seconds(xml_text: str) -> dict[str, float]:
    """The summed time of every test of each test module, from a JUnit XML report."""
    seconds: dict[str, float] = {}
    for case in ET.fromstring(xml_text).iter("testcase"):
        module = case.get("classname", "").split(".")
        name = ".".join(module[:2]) if module[:1] == ["tests"] else module[0]
        seconds[name] = seconds.get(name, 0.0) + float(case.get("time") or 0)
    return seconds


def summary_counts(output: str) -> dict[str, int]:
    """The counts of pytest's last summary line, by singular outcome name; ``{}`` without one."""
    for line in reversed(output.splitlines()):
        match = _SUMMARY.match(line.strip())
        if match:
            counts = {}
            for part in match["counts"].split(", "):
                number, word = part.split(" ")
                counts[_PLURALS.get(word, word)] = int(number)
            return counts
    return {}


def collected_counts(output: str) -> dict[str, int]:
    """What ``--collect-only`` collected and selected; ``{}`` without its summary line."""
    for line in reversed(output.splitlines()):
        match = _COLLECTED.match(line.strip())
        if match:
            selected = int(match["selected"])
            return {
                "collected": int(match["collected"] or selected),
                "selected": selected,
                "deselected": int(match["deselected"] or 0),
            }
    return {}


def reconcile(reference: dict[str, str], run: dict[str, str]) -> dict[str, list]:
    """What ``run`` lost, added or changed against ``reference``; all empty when they agree."""
    return {
        "missing": sorted(set(reference) - set(run)),
        "extra": sorted(set(run) - set(reference)),
        "changed": sorted(
            [test_id, reference[test_id], run[test_id]]
            for test_id in set(reference) & set(run)
            if reference[test_id] != run[test_id]
        ),
    }


def p95(values: list[float]) -> float:
    """The nearest-rank 95th percentile: the slowest run when there are fewer than 20."""
    ordered = sorted(values)
    return ordered[math.ceil(0.95 * len(ordered)) - 1]


@dataclass
class Run:
    config: str
    repeat: int
    seconds: float
    returncode: int
    counts: dict[str, int]
    load_before: tuple[float, float, float]
    load_after: tuple[float, float, float]
    junit: str
    log: str
    # What the plugin recorded; ``None`` when the run left no record of it.
    mode: dict[str, object] | None = None
    # Against the reference run, filled in once it is known.
    differences: dict[str, list] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return self.returncode == 0 and not any(self.differences.values())


def run_pytest(
    config: Config,
    repeat: int,
    out: Path,
    extra: list[str],
    *,
    cwd: Path = REPO_ROOT,
) -> Run:
    """One timed ``pytest`` process of ``config``, its report, mode and log under ``out``."""
    stem = f"{config.name}-{repeat}"
    junit, mode, log = out / f"{stem}.xml", out / f"{stem}.mode.json", out / f"{stem}.log"
    # An earlier attempt's report and mode are not this run's, which may leave none.
    junit.unlink(missing_ok=True)
    mode.unlink(missing_ok=True)
    argv = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:cacheprovider",
        "-p",
        PLUGIN,
        f"{MODE_OPTION}={mode}",
        f"--junitxml={junit}",
        *config.pytest_args(),
        *extra,
    ]
    load_before = os.getloadavg()
    start = time.monotonic()
    with log.open("w", encoding="utf-8") as stream:
        completed = subprocess.run(
            argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stream, stderr=subprocess.STDOUT
        )
    seconds = time.monotonic() - start
    return Run(
        config=config.name,
        repeat=repeat,
        seconds=round(seconds, 2),
        returncode=completed.returncode,
        counts=summary_counts(log.read_text(encoding="utf-8", errors="replace")),
        load_before=load_before,
        load_after=os.getloadavg(),
        junit=str(junit),
        log=str(log),
        mode=read_mode(mode),
    )


def read_mode(path: Path) -> dict[str, object] | None:
    """The mode the plugin recorded in ``path``; ``None`` when there is none that parses."""
    try:
        mode = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return mode if isinstance(mode, dict) else None


def read_outcomes(junit: Path) -> dict[str, str] | None:
    """:func:`junit_outcomes` of a run's report; ``None`` when it left none that parses.

    pytest writes the report as the session ends: a run that could not start
    (an unknown argument, a plugin that would not load) or was killed has none.
    """
    try:
        return junit_outcomes(junit.read_text(encoding="utf-8"))
    except (OSError, ValueError, ET.ParseError):
        return None


def load_run(path: Path) -> Run:
    """A run :func:`main` recorded in ``path``."""
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("differences", None)
    for key in ("load_before", "load_after"):
        data[key] = tuple(data[key])
    return Run(**data)


def kept_run(record: Path) -> tuple[Run, dict[str, str]] | None:
    """The run ``record`` holds and its report's outcomes; ``None`` unless both can be read."""
    try:
        run = load_run(record)
    except (OSError, ValueError, TypeError, KeyError):
        return None
    outcomes = read_outcomes(Path(run.junit))
    return None if outcomes is None else (run, outcomes)


def _command(*argv: str) -> str:
    try:
        completed = subprocess.run(argv, capture_output=True, text=True, cwd=REPO_ROOT)
    except OSError as exc:
        return f"unavailable ({exc.strerror})"
    return completed.stdout.strip()


def _version(distribution: str) -> str:
    try:
        return metadata.version(distribution)
    except metadata.PackageNotFoundError:
        return "not installed"


def _mem_total() -> str:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return line.split(":", 1)[1].strip()
    except OSError:
        pass
    return "unknown"


def _machine_id() -> str:
    for path in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            return Path(path).read_text(encoding="utf-8").strip()
        except OSError:
            pass
    return ""


# machine-id(5): the ID is confidential, and an application that needs a stable
# identifier of the machine records a hash of it keyed with its own fixed key.
_HOST_KEY = b"autoforge tests/parallel_bench host"


def host_id(machine_id: str, hostname: str) -> str:
    """The machine, by a keyed hash of its machine ID and host name.

    The machine ID tells installations apart, the host name machines without
    one; a change in either is another host.
    """
    message = f"{machine_id}\0{hostname}".encode()
    return hmac.new(_HOST_KEY, message, hashlib.sha256).hexdigest()[:16]


def cpu_model(cpuinfo: str) -> str:
    """Each CPU model ``/proc/cpuinfo`` names (``model name``); ``unknown`` without one."""
    models = {
        value.strip()
        for key, sep, value in (line.partition(":") for line in cpuinfo.splitlines())
        if sep and key.strip() == "model name"
    }
    return "; ".join(sorted(models)) or "unknown"


def _cpuinfo() -> str:
    try:
        return Path("/proc/cpuinfo").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def provenance() -> dict[str, object]:
    """Where and on what the numbers were measured."""
    affinity = sorted(os.sched_getaffinity(0))
    return {
        "measured_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "commit": _command("git", "rev-parse", "HEAD"),
        "python": platform.python_version(),
        "pytest": _version("pytest"),
        "pytest_xdist": _version("pytest-xdist"),
        "host_id": host_id(_machine_id(), platform.node()),
        "cpu_model": cpu_model(_cpuinfo()),
        "cpus": os.cpu_count(),
        "cpus_usable": len(affinity),
        "cpu_affinity": affinity,
        "memory": _mem_total(),
        "platform": platform.platform(),
        "git": _command("git", "--version"),
        "pytest_addopts": os.environ.get("PYTEST_ADDOPTS", ""),
        "tmpdir": tempfile.gettempdir(),
    }


# What two sessions on one --out must share for their runs to be comparable. The
# source is the commit alone, as a worktree with changes is refused. The host is
# the machine and its CPU model as well as the facts below, which two machines
# with CPUs of different speeds can share. The CPUs are the set this process may
# run on, not only how many: two sets of one size can differ in which cores share
# a physical one. The temporary directory holds every test's tmp_path: its file
# system and the length of its path change what runs.
COMPARABLE = (
    "commit",
    "python",
    "pytest",
    "pytest_xdist",
    "host_id",
    "cpu_model",
    "cpus",
    "cpu_affinity",
    "memory",
    "platform",
    "git",
    "pytest_addopts",
    "tmpdir",
    "pytest_args",
)


class UnreadableWorktree(Exception):
    """``git`` could not say what the worktree holds."""


def _git(*args: str) -> str:
    """What ``git`` prints; :class:`UnreadableWorktree` when it cannot run or fails."""
    command = " ".join(("git", *args))
    try:
        completed = subprocess.run(["git", *args], capture_output=True, text=True, cwd=REPO_ROOT)
    except OSError as exc:
        raise UnreadableWorktree(f"`{command}` could not run: {exc.strerror}") from exc
    if completed.returncode:
        detail = "; ".join(completed.stderr.strip().splitlines()) or "no message"
        raise UnreadableWorktree(f"`{command}` exited {completed.returncode}: {detail}")
    return completed.stdout


def source() -> tuple[str, list[str]]:
    """The commit checked out, and each path ``git status`` lists beside it.

    Changed, staged and untracked paths are listed (ignored ones are not): an
    untracked test module is collected like any other. Raises
    :class:`UnreadableWorktree` when git fails: a status that could not be
    read lists nothing, and that is no clean worktree.
    """
    return (
        _git("rev-parse", "HEAD").strip(),
        _git("--no-optional-locks", "status", "--porcelain").splitlines(),
    )


def source_problem(commit: str) -> str:
    """Why the worktree is not ``commit`` with nothing beside it; ``""`` when it is."""
    try:
        head, changes = source()
    except UnreadableWorktree as exc:
        return (
            f"the worktree could not be read ({exc}): a benchmark measures a commit "
            "it has seen to have nothing changed, staged or untracked"
        )
    if not head or head != commit:
        return f"HEAD is {head or 'unknown'}, not the measured commit {commit or 'unknown'}"
    if changes:
        listed = ", ".join(line.split(maxsplit=1)[-1] for line in changes[:5])
        more = f" and {len(changes) - 5} more" if len(changes) > 5 else ""
        return (
            f"the worktree has uncommitted changes ({listed}{more}): "
            "a benchmark measures a commit with nothing changed, staged or untracked"
        )
    return ""


def collect(extra: list[str]) -> dict[str, int]:
    """A ``--collect-only`` run, which also compiles the bytecode; what it collected."""
    warmup = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "no:cacheprovider", "--collect-only", *extra],
        cwd=REPO_ROOT,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    return collected_counts(warmup.stdout)


def summarize(runs: list[Run], configs: list[Config]) -> list[dict[str, object]]:
    """One row per configuration: its runs, how many were clean, and the clean ones' times.

    Only a clean run is timed. One that failed, or lost or changed a test,
    ran another suite than the reference, often a shorter one, and would make
    its configuration look faster; the ``clean`` column says how many runs
    were timed and the report names what the others did. A configuration
    with no clean run has no median, p95, min or speedup (``None``), and
    without a clean serial run no configuration has a speedup.
    """
    serial = [r.seconds for r in runs if r.config == SERIAL and r.clean]
    serial_median = statistics.median(serial) if serial else None
    rows = []
    for config in configs:
        mine = [r for r in runs if r.config == config.name]
        seconds = [r.seconds for r in mine if r.clean]
        median = statistics.median(seconds) if seconds else None
        rows.append(
            {
                "config": config.name,
                "runs": len(mine),
                "clean": len(seconds),
                "median": round(median, 1) if median is not None else None,
                "p95": round(p95(seconds), 1) if seconds else None,
                "min": round(min(seconds), 1) if seconds else None,
                "speedup": (
                    round(serial_median / median, 2)
                    if serial_median is not None and median is not None
                    else None
                ),
            }
        )
    return rows


def _cell(value: object, suffix: str = "") -> str:
    """A table cell: ``—`` for a time that was withheld."""
    return "—" if value is None else f"{value}{suffix}"


def report(rows: list[dict[str, object]], runs: list[Run], facts: dict[str, object]) -> str:
    """The Markdown report: provenance, the table, then every run and what it did not match."""
    lines = ["## Provenance", ""]
    lines += [f"- {key}: `{value}`" for key, value in facts.items()]
    lines += [
        "",
        "## Results (wall seconds of the clean runs)",
        "",
        "| config | runs | clean | median | p95 | min | speedup |",
        "|---|---|---|---|---|---|---|",
    ]
    for row in rows:
        lines.append(
            f"| {row['config']} | {row['runs']} | {row['clean']} | {_cell(row['median'])} "
            f"| {_cell(row['p95'])} | {_cell(row['min'])} | {_cell(row['speedup'], 'x')} |"
        )
    lines += ["", "## Runs", ""]
    for run in runs:
        counts = ", ".join(f"{n} {word}" for word, n in sorted(run.counts.items()))
        lines.append(
            f"- {run.config} #{run.repeat}: {run.seconds}s, exit {run.returncode}, {counts}; "
            f"load {run.load_before[0]:.2f} -> {run.load_after[0]:.2f}"
        )
        for kind, items in run.differences.items():
            for item in items[:20]:
                lines.append(f"  - {kind}: {item}")
            if len(items) > 20:
                lines.append(f"  - {kind}: ... {len(items) - 20} more")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tests.parallel_bench", description=__doc__)
    parser.add_argument("--config", action="append", dest="configs", required=True)
    parser.add_argument("--once", action="append", default=[], help="first round only")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("extra", nargs="*", help="further pytest arguments, after --")
    args = parser.parse_args(argv)
    try:
        configs = [parse_config(name) for name in args.configs]
        once = [parse_config(name) for name in args.once]
    except ValueError as exc:
        parser.error(str(exc))
    names = [c.name for c in configs + once]
    if SERIAL not in names:
        parser.error("the serial configuration is the reference every run is reconciled with")
    if len(set(names)) != len(names):
        parser.error("a configuration is given more than once")
    if args.repeats < 1:
        parser.error("--repeats must be at least 1")
    if args.out.resolve().is_relative_to(REPO_ROOT):
        parser.error(f"--out must lie outside {REPO_ROOT}, the worktree it measures")
    facts = {**provenance(), "pytest_args": args.extra}
    recorded = args.out / "provenance.json"
    if recorded.exists():
        before = json.loads(recorded.read_text(encoding="utf-8"))
        changed = [key for key in COMPARABLE if before.get(key) != facts[key]]
        if changed:
            parser.error(f"{args.out} holds runs measured with another {', '.join(changed)}")
        facts = before
    problem = source_problem(str(facts["commit"]))
    if problem:
        parser.error(problem)
    if not recorded.exists():
        args.out.mkdir(parents=True, exist_ok=True)
        facts["collected"] = collect(args.extra)
        recorded.write_text(json.dumps(facts, indent=2) + "\n", encoding="utf-8")
    runs: list[Run] = []
    reports: list[dict[str, str] | None] = []
    for repeat in range(args.repeats):
        for config in configs + (once if repeat == 0 else []):
            record = args.out / f"{config.name}-{repeat}.json"
            kept = kept_run(record)
            if kept is not None:
                run, outcomes = kept
                note = " (kept)"
            else:
                # Recorded is only a run whose report reads: any other runs again.
                record.unlink(missing_ok=True)
                run = run_pytest(config, repeat, args.out, args.extra)
                problem = source_problem(str(facts["commit"]))
                if problem:
                    parser.exit(2, f"{parser.prog}: error: {problem}; {record.stem} not recorded\n")
                outcomes = read_outcomes(Path(run.junit))
                # A run that left anything to read ran as its configuration names, or
                # whatever made it run otherwise makes every later run of it do the same.
                if run.mode is None and outcomes is not None:
                    problem = (
                        f"{record.stem} left a JUnit report but no record of how pytest-xdist "
                        f"ran it: the {PLUGIN} plugin did not run, or could not write"
                    )
                elif run.mode is not None and run.mode != config.mode():
                    problem = (
                        f"{record.stem} ran {describe_mode(run.mode)}, not as {config.name}: "
                        "something besides the configuration set pytest-xdist's options "
                        "(PYTEST_ADDOPTS, the pytest arguments after --, the repository's addopts)"
                    )
                if problem:
                    parser.exit(2, f"{parser.prog}: error: {problem}; {record.stem} not recorded\n")
                if outcomes is None:
                    note = " (no JUnit report: not recorded, runs again on resume)"
                else:
                    partial = record.with_suffix(".partial")
                    partial.write_text(json.dumps(asdict(run), indent=2) + "\n", encoding="utf-8")
                    partial.replace(record)
                    note = ""
            print(
                f"{config.name} #{repeat}: {run.seconds}s exit {run.returncode}{note}",
                file=sys.stderr,
                flush=True,
            )
            runs.append(run)
            reports.append(outcomes)
    # A serial run that failed is no reference: a run that passed would differ from it.
    reference_run, reference = next(
        (
            (run, outcomes)
            for run, outcomes in zip(runs, reports, strict=True)
            if run.config == SERIAL and run.returncode == 0 and outcomes is not None
        ),
        (None, None),
    )
    slowest: list[tuple[str, float]] = []
    for run, outcomes in zip(runs, reports, strict=True):
        if outcomes is None:
            run.differences = {"unreconciled": [f"no readable JUnit report: see {run.log}"]}
        elif reference is None:
            run.differences = {"unreconciled": ["no serial run passed to reconcile it with"]}
        else:
            run.differences = reconcile(reference, outcomes)
    if reference_run is None:
        facts["reference"] = "none: no serial run passed"
    else:
        facts["reference"] = f"{SERIAL} #{reference_run.repeat}"
        slowest = sorted(
            junit_file_seconds(Path(reference_run.junit).read_text(encoding="utf-8")).items(),
            key=lambda item: -item[1],
        )[:15]
    facts["slowest_modules_serial"] = [f"{name} {secs:.0f}s" for name, secs in slowest]
    rows = summarize(runs, configs + once)
    (args.out / "results.json").write_text(
        json.dumps(
            {"provenance": facts, "summary": rows, "runs": [asdict(r) for r in runs]}, indent=2
        )
        + "\n",
        encoding="utf-8",
    )
    sys.stdout.write(report(rows, runs, facts))
    return 0 if all(r.clean for r in runs) else 1


if __name__ == "__main__":
    sys.exit(main())
