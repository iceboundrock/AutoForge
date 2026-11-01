"""EPIC #338's exit audit stays true (docs/test-suite-performance-audit.md, #338).

The audit takes each item of EPIC #338's exit checklist, E1 to E6, and each
of the EPIC's required invariants, I1 to I7, with a verdict and the tests on
`main` that hold what the repository can show of it. These tests read both
lists from the document and fail when

- a list does not take its items once each, in order;
- an item has no verdict of its list's, or one met or held, wholly or in
  part, names no test;
- a test either list names is not a test function of its module any more.

So a test that is renamed or deleted takes the audit with it, instead of
leaving it citing coverage the suite no longer has. Whether each named test
passes is the suite's own business: they run in it. The audit's links, and
every link into it, must reach a file and a heading that exist.

Section 1's measurement is held to its runs' records
(``tests/fixtures/suite_performance_audit/``): every number it gives is the
one ``tests/suite_comparison.py`` computes from the six JUnit reports and
``removals.tsv``, and the records say what the audit says of how the runs
were made.

Two things the audit relies on were held by documents alone, and the last
tests here hold them: that workers stay opt-in, so every gate runs the suite
serially, whatever worker count make inherits (E2), and that the regression
policy `docs/development.md` publishes is the one `tests/ci_durations.py`
applies (E6).
"""

from __future__ import annotations

import re
import statistics
from collections import Counter
from functools import cache
from pathlib import Path

import pytest

from tests import ci_durations, suite_comparison

from .test_ci_workflow import _code, _job, _make_dry_run, _make_rules
from .test_removal_attestation import _bullets, _section, _test_functions
from .test_troubleshooting_guide import _broken, _links, _markdown_files

REPO_ROOT = Path(__file__).resolve().parent.parent
AUDIT = REPO_ROOT / "docs" / "test-suite-performance-audit.md"
DEVELOPMENT = REPO_ROOT / "docs" / "development.md"

EXIT_CHECKLIST = "## 2. The exit checklist"
INVARIANTS = "## 3. The required invariants"
# Each list's items: its letter, how many the EPIC's body has, and the
# verdicts it may give. Only the last verdict of each may name no test: what
# it leaves is GitHub's or the operator's (the audit's section 5).
LISTS = {
    EXIT_CHECKLIST: ("E", 6, ("Met", "Met in part", "Open")),
    INVARIANTS: ("I", 7, ("Held", "Held in part", "Not shown")),
}
# An item's subject: its letter and number, then the EPIC's words.
_ITEM = re.compile(r"^(?P<letter>[A-Z])(?P<number>[0-9]+)\. \S")
# The verdict, in italics right after the bold subject.
_VERDICT = re.compile(r"^- \*\*.+?\*\* \*(?P<verdict>[^*]+)\.\*")


@cache
def _audit() -> str:
    return AUDIT.read_text(encoding="utf-8")


def items(text: str, heading: str) -> dict[str, tuple[str | None, list[tuple[str, str]]]]:
    """The bold-opened bullets of ``heading``'s section: each item, with its
    verdict (``None`` when it has none) and the tests (module, function) it names."""
    found: dict[str, tuple[str | None, list[tuple[str, str]]]] = {}
    for subject, (opening, tests) in _bullets(text, heading).items():
        verdict = _VERDICT.match(opening)
        found[subject] = (verdict.group("verdict") if verdict else None, tests)
    return found


def _named_tests() -> list[tuple[str, str]]:
    """Every test either list names, once."""
    named = {
        ref for heading in LISTS for _, refs in items(_audit(), heading).values() for ref in refs
    }
    return sorted(named)


@pytest.mark.parametrize("heading", LISTS, ids=["exit-checklist", "invariants"])
def test_each_list_takes_the_epics_items_once_each_in_order(heading: str):
    """E1 to E6 are the exit checklist's six items and I1 to I7 the seven
    required invariants, each in the body's order and each once (a second
    bullet for one is refused by the reader)."""
    letter, count, _ = LISTS[heading]
    numbers = []
    for subject in items(_audit(), heading):
        numbered = _ITEM.match(subject)
        assert numbered and numbered.group("letter") == letter, (
            f"{subject!r} does not open with its number, {letter}<n>."
        )
        numbers.append(int(numbered.group("number")))
    assert numbers == list(range(1, count + 1))


@pytest.mark.parametrize("heading", LISTS, ids=["exit-checklist", "invariants"])
def test_every_item_has_a_verdict_and_one_met_or_held_names_a_test(heading: str):
    """An item the audit calls met or held, wholly or in part, names a test
    that holds what the repository can show of it; only the last verdict of
    its list may name none, its evidence being GitHub's."""
    _, _, verdicts = LISTS[heading]
    for subject, (verdict, tests) in items(_audit(), heading).items():
        assert verdict in verdicts, f"{subject!r}: verdict {verdict!r}"
        assert tests or verdict == verdicts[-1], f"{subject!r} is {verdict} and names no test"


@pytest.mark.parametrize("module, function", _named_tests(), ids=lambda value: value)
def test_every_test_the_audit_names_exists(module: str, function: str):
    """A renamed or deleted test fails here, naming the module it was in."""
    assert (REPO_ROOT / module).is_file(), f"{module} does not exist"
    assert function in _test_functions(module), f"{module} defines no {function}"


def test_the_audits_links_and_every_link_into_it_resolve():
    """The audit points at sections of other documents for its evidence, and
    others point at it; a renamed heading would leave either pointing at the
    top of a page."""
    broken = [
        problem
        for target in _links(AUDIT)
        if "://" not in target
        for problem in [_broken(AUDIT, target)]
        if problem
    ]
    linked_from = []
    for source in _markdown_files():
        for target in _links(source):
            file_part = target.partition("#")[0]
            if "://" in target or not file_part:
                continue
            if (source.parent / file_part).resolve() == AUDIT:
                linked_from.append(source)
                broken += [problem for problem in [_broken(source, target)] if problem]
    assert broken == []
    assert REPO_ROOT / "docs" / "README.md" in linked_from


def test_the_reader_takes_a_verdict_only_in_italics_after_the_item():
    """The parser itself: a verdict is read from the item's opening line
    alone, in italics right after it; one in the text below, or in bold, is
    no verdict; a bullet names only backticked ``tests/...::test_...``
    references, and nothing past the section's end is read."""
    text = (
        f"{INVARIANTS}\n\n"
        "- **I1. One.** *Held.* `tests/test_a.py::test_a`, tests/test_a.py::test_c.\n"
        "- **I2. Two.** *Held in part.* `tests/test_a.py::test_b`;\n"
        "  the rest is the operator's.\n"
        "- **I3. Three.** *Not shown.* GitHub's.\n"
        "- **I4. Four.** Held, said in passing.\n  *Held.*\n"
        "- **I5. Five.** **Held.**\n"
        "## 4. Next\n- **I6. Not the list.** *Held.*\n"
    )
    assert items(text, INVARIANTS) == {
        "I1. One.": ("Held", [("tests/test_a.py", "test_a")]),
        "I2. Two.": ("Held in part", [("tests/test_a.py", "test_b")]),
        "I3. Three.": ("Not shown", []),
        "I4. Four.": (None, []),
        "I5. Five.": (None, []),
    }
    with pytest.raises(AssertionError, match="attested twice"):
        items(f"{INVARIANTS}\n- **I1. A.** *Held.*\n- **I1. A.** *Held.*\n", INVARIANTS)


# -- section 1 and its runs' records (R1-F3, R1-F4 of PR #432's review) --------

SECTION_1 = "## 1. The suite before and after, on one host"
# The commits that carry the EPIC's work items and the two fixes its
# measurements relied on (I1).
EPIC_COMMITS = (
    "6fddbc9",
    "1a9424e",
    "7fa0786",
    "b14f959",
    "3d836ee",
    "acebe52",
    "cdb9ac0",
    "40385a8",
)
# A program a runner script could reach GitHub, a git remote or a package index with.
_NETWORK_TOOL = re.compile(r"(?<![\w./-])(?:gh|git|uv|curl|wget)(?![\w.-])")


@cache
def _runs() -> tuple[dict[str, list[suite_comparison.Run]], list[suite_comparison.Event]]:
    return suite_comparison.read_archive()


def _section_1() -> str:
    """Section 1 of the audit, up to the next heading of its level outside a
    code fence: its code holds lines that open with ``#``, which
    ``_section`` would take for a heading."""
    lines = _audit().splitlines()
    body, fenced = [], False
    for line in lines[lines.index(SECTION_1) + 1 :]:
        if line.startswith("```"):
            fenced = not fenced
        elif not fenced and line.startswith("## "):
            break
        body.append(line)
    return "\n".join(body)


def _prose(text: str) -> str:
    """``text`` with every run of whitespace one space, as a sentence reads across lines."""
    return " ".join(text.split())


def test_section_1_is_what_the_runs_records_say():
    """Every number section 1 gives is the one ``tests/suite_comparison.py``
    computes from the six JUnit reports, GNU time's figures and pytest's
    summaries in the archive: the results table, the shares, the tests in
    common and their time, the modules' table, the spread of each commit's
    runs and the tests that left. A run whose outcomes differ from the first
    of its commit, or that failed a test, is no measurement."""
    runs, _ = _runs()
    before, after = runs["pre"], runs["post"]
    section = _section_1()
    lines, prose = section.splitlines(), _prose(section)
    for side in suite_comparison.SIDES:
        assert suite_comparison.results_row(side, runs[side]) in lines
        for name, found in suite_comparison.differences(runs[side]).items():
            assert found == {"missing": [], "extra": [], "changed": []}, name
        for run in runs[side]:
            assert set(run.outcomes().values()) <= {"passed", "skipped"}, run.name
            assert set(run.counts) == {"passed", "skipped", "deselected"}, run.name
    # The baseline's counts are those of the CI run the EPIC's body cites.
    assert before[0].counts == {"passed": 11407, "skipped": 18, "deselected": 1}

    ids_before, ids_after = set(before[0].tests), set(after[0].tests)
    common = ids_before & ids_after
    wall_before = statistics.median(run.wall for run in before)
    wall_after = statistics.median(run.wall for run in after)
    own_before = statistics.median(run.seconds(common) for run in before)
    own_after = statistics.median(run.seconds(common) for run in after)
    spread = [max(r.wall for r in runs[s]) - min(r.wall for r in runs[s]) for s in ("pre", "post")]
    for words in (
        f"takes {(1 - wall_after / wall_before) * 100:.1f}% less wall time",
        f"running {(len(ids_after) / len(ids_before) - 1) * 100:.1f}% more tests",
        f"{len(ids_after):,} test ids against {len(ids_before):,}",
        f"{wall_after / len(ids_after) * 1000:.0f} ms a test against "
        f"{wall_before / len(ids_before) * 1000:.0f}",
        f"{len(common):,} test ids are in both suites",
        f"from a median of {own_before:.1f} s to {own_after:.1f} s, "
        f"{(1 - own_after / own_before) * 100:.0f}% less",
        f"within {spread[0]:.2f} s of each other at the baseline and {spread[1]:.2f} s on `main`",
    ):
        assert words in prose, words
    gains = suite_comparison.module_gains(before, after, common)[: suite_comparison.TOP_MODULES]
    assert [line for line in lines if line.startswith("| `tests/")] == (
        suite_comparison.module_rows(gains)
    )

    kept, went = suite_comparison.split_gone(before[0], after[0])
    assert {before[0].tests[t][0] for t in kept + went} == {"passed"}
    by_module = Counter(suite_comparison.module_of(t) for t in kept).most_common(2)
    (first, in_first), (second, in_second) = by_module
    for words in (
        f"{len(kept) + len(went):,} test ids of `1f6abad` are not in `d0948fb`'s suite",
        f"{len(kept)} are parametrizations of test functions that are still there",
        f"{in_first} in `{first}` and {in_second} in `{second}`",
        f"The other {len(went)} belong to "
        f"{len({suite_comparison.function_of(t) for t in went})} test functions that left their "
        "module",
    ):
        assert words in prose, words


def test_the_removals_name_each_function_that_went_and_section_1s_tallies():
    """``removals.tsv`` names exactly the test functions the baseline ran and
    ``main`` does not have, each with a commit between the two, and the
    commits' tallies are section 1's: LOCAL's removal (#387, #388) and the two
    changes of behaviour, and no commit of the EPIC. ``python -m
    tests.suite_comparison --git .`` checks the file against the history."""
    runs, _ = _runs()
    _, went = suite_comparison.split_gone(runs["pre"][0], runs["post"][0])
    removals = suite_comparison.read_removals()
    assert set(removals) == {suite_comparison.function_of(t) for t in went}
    assert all(re.fullmatch(r"[0-9a-f]{40}", commit) for commit in removals.values())
    tally = Counter(removals[suite_comparison.function_of(t)][:7] for t in went)
    assert set(tally) == {"aee704e", "3eb68ac", "9a7d8f9", "a86b729"}
    assert not set(tally) & set(EPIC_COMMITS)
    prose = _prose(_section_1())
    local = tally["aee704e"] + tally["3eb68ac"]
    assert tally["9a7d8f9"] + tally["a86b729"] == len(went) - local == 3
    # Of LOCAL's, those whose function has a namesake in another module on
    # main: moved or rewritten there by the same commits.
    names = {suite_comparison.function_of(t).split("::")[1] for t in runs["post"][0].tests}
    moved = [t for t in went if suite_comparison.function_of(t).split("::")[1] in names]
    assert {removals[suite_comparison.function_of(t)][:7] for t in moved} <= {"aee704e", "3eb68ac"}
    for words in (
        f"LOCAL's removal took {local} of the {len(went)} from theirs, {tally['aee704e']} with "
        f"#387 (`aee704e`) and {tally['3eb68ac']} with #388 (`3eb68ac`)",
        f"Of those {local}, {len(moved)} ids, in "
        f"{len({suite_comparison.function_of(t) for t in moved})} functions, have a namesake "
        "in another module on `main`",
        "Three went where #283 (`9a7d8f9`) and #349 (`a86b729`)",
        "No commit of this EPIC removed one (I1)",
    ):
        assert words in prose, words
    invariants = _prose(_section(_audit(), INVARIANTS))
    assert all(f"`{commit}`" in invariants for commit in EPIC_COMMITS)


def test_the_records_say_how_the_runs_were_made():
    """What section 1 and I7 say of how the six runs were made is in their
    records: alternating before and after, every one exiting 0, at the times
    and load averages section 1.1 gives; the live test, the suite's one model
    call, deselected and the five opt-in probes skipped in each, with only
    the tests that need the missing zsh skipped besides and every sandbox
    test run; the baseline changed in one test helper alone; and a runner
    script that starts pytest and no tool that reaches the network."""
    runs, events = _runs()
    section = _section_1()
    prose = _prose(section)
    starts = [(e.side, e.run) for e in events if e.edge == "start"]
    assert starts == [(side, n) for n in suite_comparison.RUNS for side in ("pre", "post")]
    assert [(e.side, e.run, e.exit_status) for e in events if e.edge == "end"] == [
        (side, n, 0) for side, n in starts
    ]
    first, last = events[0].at, events[-1].at
    assert f"On {first[:10]} between {first[11:16]} and {last[11:16]} UTC" in prose
    assert f"at most {max(e.load for e in events):.2f} at the start or end of a run" in prose

    zsh: dict[str, set[int]] = {}
    for side in suite_comparison.SIDES:
        for run in runs[side]:
            assert run.counts["deselected"] == 1, run.name
            assert not [t for t in run.tests if t.startswith("tests.test_live_pi::")], run.name
            reasons = run.skips.values()
            probes = [r for r in reasons if "zsh" not in r]
            assert len(probes) == 5 and all("AUTOFORGE_" in r for r in probes), run.name
            assert not [t for t in run.skips if "sandbox" in t.split("::")[0]], run.name
            zsh.setdefault(side, set()).add(len(reasons) - len(probes))
    (before,), (after,) = zsh["pre"], zsh["post"]
    assert f"({before} at the baseline, {after} on `main`)" in prose
    assert "the five opt-in probes" in prose

    patch = suite_comparison.read_member("pre-259.patch")
    assert [line for line in patch.splitlines() if line.startswith("diff --git ")] == [
        "diff --git a/tests/test_interaction.py b/tests/test_interaction.py"
    ]
    bench = suite_comparison.read_member("bench.sh")
    assert "-m pytest -p no:cacheprovider --junitxml=" in bench
    assert "-m pytest -p no:cacheprovider --junitxml=<run>.xml" in section
    assert not _NETWORK_TOOL.findall(bench)
    for tool in ("bench.sh -- uv run pytest", "git fetch", "curl -s", "x=$(gh pr view)"):
        assert _NETWORK_TOOL.search(tool), tool


def test_removals_reads_the_last_commit_that_removed_a_function_from_its_module():
    """The attribution itself, on a ``git log -p`` of three commits: a
    function's removal is its last commit's, a ``def`` moved within its
    module is no removal, and one deleted with its module is."""
    log = "\n".join(
        [
            "commit " + "a" * 40,
            "",
            "diff --git a/tests/test_x.py b/tests/test_x.py",
            "--- a/tests/test_x.py",
            "+++ b/tests/test_x.py",
            "-def test_gone():",
            "-def test_moved(tmp_path):",
            "+def test_moved(tmp_path):",
            "-    def test_in_a_class(self):",
            "+def test_back():",
            "commit " + "b" * 40,
            "",
            "diff --git a/tests/test_x.py b/tests/test_x.py",
            "-def test_back():",
            "diff --git a/tests/test_y.py b/tests/test_y.py",
            "deleted file mode 100644",
            "--- a/tests/test_y.py",
            "+++ /dev/null",
            "-async def test_with_its_module():",
            "-def helper():",
        ]
    )
    removed = suite_comparison.removals(log)
    assert removed == {
        ("tests/test_x.py", "test_gone"): "a" * 40,
        ("tests/test_x.py", "test_in_a_class"): "a" * 40,
        ("tests/test_x.py", "test_back"): "b" * 40,
        ("tests/test_y.py", "test_with_its_module"): "b" * 40,
    }
    functions = ["tests.test_x.TestC::test_in_a_class", "tests.test_x::test_moved"]
    assert suite_comparison.attribute(functions, removed) == {functions[0]: "a" * 40}


# -- what only documents held (E2, E6) -----------------------------------------

# Options that put pytest on pytest-xdist's workers, or load xdist explicitly.
_WORKER_OPTION = re.compile(r"(?:^|\s)(?:-n\s*\S|--numprocesses\b|--dist\b|-p\s*xdist\b)")
# Every gate that runs the suite.
_GATES = ("test", "check-matrix", "test-sandbox", "test-sandbox-measured")
# Where a make started under this suite reads a worker count or variables
# from besides its own command line: the environment, and the command-line
# assignments and flags a make running the suite hands its recipe
# (`make test-parallel PYTEST_WORKERS=2` passes `PYTEST_WORKERS=2` both ways).
_INHERITED_BY_MAKE = (
    "PYTEST_WORKERS",
    "MAKEFLAGS",
    "MFLAGS",
    "GNUMAKEFLAGS",
    "MAKEOVERRIDES",
    "MAKELEVEL",
)


def _inherit_from_make(monkeypatch: pytest.MonkeyPatch, **variables: str) -> None:
    """The ``make`` that ``_make_dry_run`` starts inherits ``variables`` alone of
    :data:`_INHERITED_BY_MAKE`, whatever this run of the suite was started with."""
    for name in _INHERITED_BY_MAKE:
        monkeypatch.delenv(name, raising=False)
    for name, value in variables.items():
        monkeypatch.setenv(name, value)


def test_workers_stay_opt_in_and_every_gate_runs_the_suite_serially(
    monkeypatch: pytest.MonkeyPatch,
):
    """E2: M1 adopted workers for local use only (``docs/development.md``,
    "The suite on several workers"): ``make test-parallel`` runs the suite on
    ``PYTEST_WORKERS`` workers with ``--dist=load``, and ``make test``,
    ``make check``, ``make check-matrix`` and every CI job stay serial until
    the preconditions recorded there are met. Running CI on workers changes
    this test, a protected workflow and that page together, under a human's
    review. The default of 4 is read with no worker count inherited, so
    the suite run with ``PYTEST_WORKERS`` set passes too (PR #432 review
    R1-F1)."""
    _inherit_from_make(monkeypatch)
    assert _make_dry_run("test-parallel") == ["uv run pytest -n 4 --dist=load"]
    assert _make_dry_run("test-parallel", assignments=("PYTEST_WORKERS=8",)) == [
        "uv run pytest -n 8 --dist=load"
    ]
    rules = _make_rules()
    for target, rule in rules.items():
        assert "test-parallel" not in rule.prerequisites, f"`make {target}` runs the workers"
    for gate in _GATES:
        for command in _make_dry_run(gate):
            assert not _WORKER_OPTION.search(command), f"`make {gate}` runs {command!r}"
    code = _code()
    assert "test-parallel" not in code
    assert "PYTEST_ADDOPTS" not in code and "PYTEST_PLUGINS" not in code
    for job in ("test", "sandbox"):
        for line in _job(code, job).splitlines():
            assert not _WORKER_OPTION.search(line), f"CI's {job} job runs {line.strip()!r}"
    assert _WORKER_OPTION.search("uv run pytest -n 4 --dist=load")
    assert _WORKER_OPTION.search("uv run pytest -nauto")
    assert _WORKER_OPTION.search("uv run pytest -p xdist")
    assert not _WORKER_OPTION.search("uv run pytest -rs --junitxml=x.xml")


@pytest.mark.parametrize(
    "inherited",
    [
        {"PYTEST_WORKERS": "2"},
        {"MAKEFLAGS": " -- PYTEST_WORKERS=2"},
        {"PYTEST_WORKERS": "2", "MAKEFLAGS": "s -- PYTEST_WORKERS=2"},
    ],
    ids=["environment", "makeflags", "under-make-test-parallel"],
)
def test_an_inherited_worker_count_reaches_test_parallel_and_no_gate(
    monkeypatch: pytest.MonkeyPatch, inherited: dict[str, str]
):
    """``PYTEST_WORKERS`` is the documented override (``docs/development.md``).
    Inherited from the environment, or from the ``MAKEFLAGS`` a make running
    this suite hands it (``make test-parallel PYTEST_WORKERS=2`` passes both),
    it sets ``make test-parallel``'s workers; make's own command line still
    beats it, and every gate stays serial with it set (PR #432 review R1-F1)."""
    _inherit_from_make(monkeypatch, **inherited)
    assert _make_dry_run("test-parallel") == ["uv run pytest -n 2 --dist=load"]
    assert _make_dry_run("test-parallel", assignments=("PYTEST_WORKERS=8",)) == [
        "uv run pytest -n 8 --dist=load"
    ]
    for gate in _GATES:
        for command in _make_dry_run(gate):
            assert not _WORKER_OPTION.search(command), f"`make {gate}` runs {command!r}"


def test_the_documented_regression_policy_is_the_one_the_report_applies():
    """E6: the window, its minimum, its age limit and the three bounds that
    ``docs/development.md`` ("The regression policy") publishes are the
    constants ``tests/ci_durations.py`` decides with, so the policy an
    operator reads is the one each CI run's report applies."""
    policy = " ".join(
        _section(DEVELOPMENT.read_text(encoding="utf-8"), "### The regression policy").split()
    )
    for words in (
        f"the last {ci_durations.WINDOW} runs of a push to main in that category",
        f"recorded in the {ci_durations.MAX_AGE_DAYS} days before the run",
        f"| `regression` | above {ci_durations.REGRESSION:.2f} × the window's p95 |",
        f"| `slower` | above {ci_durations.SLOWER:.2f} × the median |",
        f"| `faster` | below {ci_durations.FASTER:.2f} × the median |",
        f"| `insufficient-baseline` | fewer than {ci_durations.MIN_SAMPLES} runs in the window |",
    ):
        assert words in policy, words
