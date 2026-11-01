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

Two things the audit relies on were held by documents alone, and the last
tests here hold them: that workers stay opt-in, so every gate runs the suite
serially (E2), and that the regression policy `docs/development.md`
publishes is the one `tests/ci_durations.py` applies (E6).
"""

from __future__ import annotations

import re
from functools import cache
from pathlib import Path

import pytest

from tests import ci_durations

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


# -- what only documents held (E2, E6) -----------------------------------------

# Options that put pytest on pytest-xdist's workers, or load xdist explicitly.
_WORKER_OPTION = re.compile(r"(?:^|\s)(?:-n\s*\S|--numprocesses\b|--dist\b|-p\s*xdist\b)")


def test_workers_stay_opt_in_and_every_gate_runs_the_suite_serially():
    """E2: M1 adopted workers for local use only (``docs/development.md``,
    "The suite on several workers"): ``make test-parallel`` runs the suite on
    ``PYTEST_WORKERS`` workers with ``--dist=load``, and ``make test``,
    ``make check``, ``make check-matrix`` and every CI job stay serial until
    the preconditions recorded there are met. Running CI on workers changes
    this test, a protected workflow and that page together, under a human's
    review."""
    assert _make_dry_run("test-parallel") == ["uv run pytest -n 4 --dist=load"]
    assert _make_dry_run("test-parallel", assignments=("PYTEST_WORKERS=8",)) == [
        "uv run pytest -n 8 --dist=load"
    ]
    rules = _make_rules()
    for target, rule in rules.items():
        assert "test-parallel" not in rule.prerequisites, f"`make {target}` runs the workers"
    for gate in ("test", "check-matrix", "test-sandbox", "test-sandbox-measured"):
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
