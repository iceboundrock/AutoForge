"""Two commits' serial suite runs, compared test by test (EPIC #338's exit audit).

No test: the reconciliation behind section 1 of
``docs/test-suite-performance-audit.md``, which anyone can run to reproduce
that section's numbers from the runs' own reports::

    uv run python -m tests.suite_comparison
    uv run python -m tests.suite_comparison --git .

:data:`ARCHIVE` holds what the six runs left, as the runner script in it,
``bench.sh``, wrote it. ``pre`` is the EPIC's baseline, :data:`BEFORE`, with
``pre-259.patch`` applied to one test helper; ``post`` is ``main`` at
:data:`AFTER`. For each side and run, 1 to 3:

- ``<side>-<run>.xml``, pytest's JUnit report, without the host name pytest
  writes into it (``tests/parallel_bench.py`` records none either);
- ``<side>-<run>.time``, GNU time's ``%e %U %S %M``: the ``pytest`` process's
  wall seconds, its and its children's user and system CPU seconds, and its
  peak resident size in KiB;
- ``<side>-<run>.log``, pytest's output, whose last line gives the counts.

``events.log`` gives each run's start and end (UTC), exit status and load
average, in the order they ran, and ``<side>-collect.log`` the
``--collect-only`` run that compiled each side's bytecode first.

The comparison reads every test of a report by id (``classname::name``) and
outcome as ``tests.parallel_bench`` does, with its own time: setup, call and
teardown, summed for an id pytest reports twice. It checks that a side's
three runs have the same tests with the same outcomes. It then gives each
side's medians, the tests both sides ran and their time, the modules whose
common tests gained most, and the tests the baseline ran that ``main`` does
not: those of a test function ``main`` still has, with other parameters, and
those of the functions that went.

Which commit removed each function that went needs the history between the
two commits, which a shallow clone lacks. :data:`REMOVALS` records it, so
that ``tests/test_suite_performance_audit.py`` checks the audit's tallies
without the history. ``--git <clone>`` derives it again from ``git log -p``
of a clone that holds both commits, and fails when it differs from
:data:`REMOVALS` (``--write-removals`` writes it instead).

It reads the archive in memory and writes nothing but :data:`REMOVALS`, and
that only when asked.
"""

from __future__ import annotations

import argparse
import re
import statistics
import subprocess
import sys
import tarfile
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from tests import parallel_bench

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "tests" / "fixtures" / "suite_performance_audit"
ARCHIVE = FIXTURES / "runs.tar.xz"
REMOVALS = FIXTURES / "removals.tsv"
BEFORE = "1f6abad5debe375594fc2ab6d13cbe5cfac96396"
AFTER = "d0948fb96b94c7be1f215ce1c29b5a4c37b935f3"
SIDES = ("pre", "post")
COMMITS = {"pre": BEFORE, "post": AFTER}
RUNS = (1, 2, 3)
# How many modules the table of the largest gains lists.
TOP_MODULES = 8
# events.log: "pre 1 start 2026-11-01T19:22:21Z load 0.10 0.23 0.18".
_EVENT = re.compile(
    r"^(?P<side>[a-z]+) (?P<run>[0-9]+) (?P<edge>start|end) (?P<at>\S+)"
    r"(?: rc (?P<rc>-?[0-9]+))? load (?P<load>[0-9.]+) "
)
# A test function's definition, added or removed, in a ``git log -p`` hunk.
_DEF = re.compile(r"^(?P<sign>[-+])\s*(?:async\s+)?def\s+(?P<name>test_\w+)\s*\(")


@dataclass(frozen=True)
class Run:
    """One run of the suite: what GNU time, pytest's summary and its JUnit report say."""

    name: str
    wall: float
    user: float
    system: float
    max_rss_kib: int
    counts: dict[str, int]
    # Every test by id: its outcome and its own seconds.
    tests: dict[str, tuple[str, float]]
    # Every skipped test by id: the reason pytest gave.
    skips: dict[str, str]

    def outcomes(self) -> dict[str, str]:
        return {test_id: outcome for test_id, (outcome, _) in self.tests.items()}

    def seconds(self, ids: Iterable[str]) -> float:
        return sum(self.tests[test_id][1] for test_id in ids)


@dataclass(frozen=True)
class Event:
    side: str
    run: int
    edge: str
    at: str
    exit_status: int | None
    load: float


def junit_tests(xml_text: str) -> dict[str, tuple[str, float]]:
    """Every test of a JUnit XML report by id, with its outcome and its summed time."""
    outcomes = parallel_bench.junit_outcomes(xml_text)
    seconds: dict[str, float] = {}
    for case in ET.fromstring(xml_text).iter("testcase"):
        test_id = f"{case.get('classname', '')}::{case.get('name', '')}"
        seconds[test_id] = seconds.get(test_id, 0.0) + float(case.get("time") or 0)
    return {test_id: (outcome, seconds[test_id]) for test_id, outcome in outcomes.items()}


def junit_skips(xml_text: str) -> dict[str, str]:
    """Every skipped test of a JUnit XML report by id, with the message of its skip."""
    skips: dict[str, str] = {}
    for case in ET.fromstring(xml_text).iter("testcase"):
        for child in case:
            if child.tag == "skipped":
                skips[f"{case.get('classname', '')}::{case.get('name', '')}"] = child.get(
                    "message", ""
                )
    return skips


def read_events(text: str) -> list[Event]:
    """``events.log``'s start and end lines, in order."""
    events = []
    for line in text.splitlines():
        match = _EVENT.match(line)
        if match:
            rc = match["rc"]
            events.append(
                Event(
                    match["side"],
                    int(match["run"]),
                    match["edge"],
                    match["at"],
                    None if rc is None else int(rc),
                    float(match["load"]),
                )
            )
    return events


def _member(archive: tarfile.TarFile, path: Path, name: str) -> str:
    member = archive.extractfile(name)
    if member is None:
        raise ValueError(f"{path}: {name} is not a regular file")
    return member.read().decode("utf-8")


def read_member(name: str, path: Path = ARCHIVE) -> str:
    """One file of the archive (``bench.sh``, ``pre-259.patch``), read without extracting it."""
    with tarfile.open(path, "r:xz") as archive:
        return _member(archive, path, name)


def read_archive(path: Path = ARCHIVE) -> tuple[dict[str, list[Run]], list[Event]]:
    """Each side's runs, in order, and the events, read from ``path`` without extracting it."""
    with tarfile.open(path, "r:xz") as archive:

        def text(name: str) -> str:
            return _member(archive, path, name)

        runs: dict[str, list[Run]] = {}
        for side in SIDES:
            runs[side] = []
            for number in RUNS:
                stem = f"{side}-{number}"
                wall, user, system, rss = text(f"{stem}.time").split()
                xml_text = text(f"{stem}.xml")
                runs[side].append(
                    Run(
                        name=stem,
                        wall=float(wall),
                        user=float(user),
                        system=float(system),
                        max_rss_kib=int(rss),
                        counts=parallel_bench.summary_counts(text(f"{stem}.log")),
                        tests=junit_tests(xml_text),
                        skips=junit_skips(xml_text),
                    )
                )
        return runs, read_events(text("events.log"))


def differences(runs: Sequence[Run]) -> dict[str, dict[str, list]]:
    """What each run after the first lost, added or changed against the first."""
    first = runs[0].outcomes()
    return {run.name: parallel_bench.reconcile(first, run.outcomes()) for run in runs[1:]}


def module_file(classname: str) -> str:
    """The module file of a JUnit classname: ``tests.test_x.TestY`` is ``tests/test_x.py``."""
    return "/".join(classname.split(".")[:2]) + ".py"


def module_of(test_id: str) -> str:
    """The module file a test id is in."""
    return module_file(test_id.split("::", 1)[0])


def function_of(test_id: str) -> str:
    """A test id without its parameters: ``classname::function``."""
    classname, name = test_id.split("::", 1)
    return f"{classname}::{name.split('[', 1)[0]}"


def median_of(runs: Sequence[Run], value) -> float:
    return statistics.median(value(run) for run in runs)


def module_gains(
    before: Sequence[Run], after: Sequence[Run], ids: Iterable[str]
) -> list[tuple[str, float, float]]:
    """Each module's median seconds over ``ids`` on either side, the largest gain first."""
    by_module: dict[str, list[str]] = {}
    for test_id in ids:
        by_module.setdefault(module_of(test_id), []).append(test_id)
    rows = [
        (
            module,
            statistics.median(run.seconds(tests) for run in before),
            statistics.median(run.seconds(tests) for run in after),
        )
        for module, tests in by_module.items()
    ]
    return sorted(rows, key=lambda row: (row[2] - row[1], row[0]))


def split_gone(before: Run, after: Run) -> tuple[list[str], list[str]]:
    """The tests only ``before`` ran: those of a function ``after`` still has, and the others."""
    kept = {function_of(test_id) for test_id in after.tests}
    gone = sorted(set(before.tests) - set(after.tests))
    return (
        [t for t in gone if function_of(t) in kept],
        [t for t in gone if function_of(t) not in kept],
    )


def removals(log_text: str) -> dict[tuple[str, str], str]:
    """Which commit last removed each test function from its module.

    ``log_text`` is ``git log --reverse -p --no-renames --format='commit %H'``
    over the test modules, oldest commit first. A commit removes a function
    from a module when a hunk of that module removes its ``def`` and none adds
    one of the same name (a function moved within its module is kept).
    Returns (module path, function name) to the commit's full SHA.
    """
    found: dict[tuple[str, str], str] = {}
    commit: str | None = None
    path: str | None = None
    removed: dict[str, set[str]] = {}
    added: dict[str, set[str]] = {}

    def settle() -> None:
        if commit is not None:
            for module, names in removed.items():
                for name in names - added.get(module, set()):
                    found[(module, name)] = commit

    for line in log_text.splitlines():
        if line.startswith("commit "):
            settle()
            commit, path, removed, added = line.split()[1], None, {}, {}
        elif line.startswith("diff --git "):
            path = line.split(" b/", 1)[1]
        elif path is not None and not line.startswith(("--- ", "+++ ")):
            match = _DEF.match(line)
            if match:
                side = removed if match["sign"] == "-" else added
                side.setdefault(path, set()).add(match["name"])
    settle()
    return found


def attribute(functions: Iterable[str], removed: dict[tuple[str, str], str]) -> dict[str, str]:
    """Each function (``classname::function``) to the commit that removed it, if one did."""
    attributed = {}
    for function in functions:
        classname, name = function.split("::", 1)
        commit = removed.get((module_file(classname), name))
        if commit is not None:
            attributed[function] = commit
    return attributed


def read_removals(path: Path = REMOVALS) -> dict[str, str]:
    """:data:`REMOVALS`: each function that went (``classname::function``) to its commit."""
    rows = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            function, commit = line.split("\t")
            rows[function] = commit
    return rows


def render_removals(attributed: dict[str, str]) -> str:
    header = (
        "# Each test function the suite ran at 1f6abad and does not have at d0948fb,\n"
        "# and the commit between the two that removed it (tests/suite_comparison.py).\n"
    )
    return header + "".join(f"{f}\t{attributed[f]}\n" for f in sorted(attributed))


def _seconds(value: float) -> str:
    return f"{value:.1f}"


def _count(value: int) -> str:
    return f"{value:,}"


def results_row(side: str, runs: Sequence[Run]) -> str:
    """A row of the audit's results table, from one side's runs."""
    counts = runs[0].counts
    rss = sorted(round(run.max_rss_kib / 1024) for run in runs)
    return (
        f"| `{COMMITS[side][:7]}` | {', '.join(_seconds(run.wall) for run in runs)} "
        f"| {_seconds(median_of(runs, lambda run: run.wall))} "
        f"| {_seconds(median_of(runs, lambda run: run.user))} "
        f"| {_seconds(median_of(runs, lambda run: run.system))} "
        f"| {rss[0]} to {rss[-1]} MiB "
        f"| {_count(counts.get('passed', 0))}, {_count(counts.get('skipped', 0))}, "
        f"{_count(counts.get('deselected', 0))} |"
    )


def module_rows(gains: Sequence[tuple[str, float, float]]) -> list[str]:
    return [f"| `{module}` | {_seconds(a)} | {_seconds(b)} |" for module, a, b in gains]


def report(runs: dict[str, list[Run]], events: Sequence[Event]) -> str:
    """Section 1's numbers, as Markdown."""
    before, after = runs["pre"], runs["post"]
    lines = ["| commit | runs, wall seconds | median | user CPU | system CPU | memory | counts |"]
    lines += ["|---|---|---|---|---|---|---|", results_row("pre", before)]
    lines += [results_row("post", after), ""]
    for side in SIDES:
        for name, found in differences(runs[side]).items():
            lost = sum(len(ids) for ids in found.values())
            lines.append(f"- {name} against {side}-1: {lost} tests lost, added or changed")
    common = set(before[0].tests) & set(after[0].tests)
    common_before = median_of(before, lambda run: run.seconds(common))
    common_after = median_of(after, lambda run: run.seconds(common))
    kept, went = split_gone(before[0], after[0])
    lines += [
        f"- test ids: {_count(len(before[0].tests))} and {_count(len(after[0].tests))}, "
        f"{_count(len(common))} in both",
        f"- their own time, median: {_seconds(common_before)} s and {_seconds(common_after)} s",
        f"- only at the baseline: {_count(len(kept) + len(went))}, of which {len(kept)} of "
        f"functions still there and {len(went)} of "
        f"{len({function_of(t) for t in went})} functions that went",
        f"- load average at a run's start or end: at most {max(e.load for e in events):.2f}",
        "",
        "| module | `1f6abad` | `d0948fb` |",
        "|---|---|---|",
        *module_rows(module_gains(before, after, common)[:TOP_MODULES]),
    ]
    return "\n".join(lines) + "\n"


def _git_log(clone: Path) -> str:
    completed = subprocess.run(
        [
            "git",
            "-C",
            str(clone),
            "log",
            "--reverse",
            "-p",
            "--no-renames",
            "--no-color",
            "--no-ext-diff",
            "--no-textconv",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            "--format=commit %H",
            f"{BEFORE}..{AFTER}",
            "--",
            "tests",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"git log failed in {clone}: {completed.stderr.strip()}")
    return completed.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m tests.suite_comparison",
        description="Reproduce section 1 of docs/test-suite-performance-audit.md from its runs.",
    )
    parser.add_argument("--archive", type=Path, default=ARCHIVE)
    parser.add_argument(
        "--git",
        type=Path,
        metavar="CLONE",
        help="a clone holding both commits: check each removed function's commit against git",
    )
    parser.add_argument(
        "--write-removals",
        action="store_true",
        help=f"with --git, write the attribution to {REMOVALS.relative_to(REPO_ROOT)}",
    )
    args = parser.parse_args(argv)
    runs, events = read_archive(args.archive)
    print(report(runs, events), end="")
    if args.git is None:
        return 0
    _, went = split_gone(runs["pre"][0], runs["post"][0])
    functions = sorted({function_of(t) for t in went})
    attributed = attribute(functions, removals(_git_log(args.git)))
    unknown = [f for f in functions if f not in attributed]
    if unknown:
        print(f"no commit removed {len(unknown)} of them: {unknown[:5]}", file=sys.stderr)
        return 1
    tallies = Counter(attributed[function_of(t)][:7] for t in went)
    print("", *(f"- {commit}: {n} test ids" for commit, n in tallies.most_common()), sep="\n")
    if args.write_removals:
        REMOVALS.write_text(render_removals(attributed), encoding="utf-8")
        return 0
    if read_removals() != attributed:
        print(f"{REMOVALS} differs from what git says", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
