"""ADR 0009's record of LOCAL mode's removal stays true (#389, #383).

LOCAL mode's removal (ADR 0009) kept every shared invariant a LOCAL test
used to protect: Appendix A.2.4 lists those invariants with their owners,
and Appendix C.6, written by S4 (#389), names for each one the tests on
`main` that hold it now. Appendix D, EPIC #383's exit audit, names for each
of the EPIC's exit criteria a verdict and the tests that hold it. These
tests read the lists from the record and fail when

- C.6 attests an invariant A.2.4 does not list, or leaves one out;
- an attestation names no test;
- D does not take the EPIC's twelve criteria once each, in order, or gives
  one no verdict, or one it calls met, wholly or in part, names no test;
- a test either names is not a test function of its module any more.

So a test that is renamed or deleted takes the record with it, instead of
leaving it attesting coverage the suite no longer has. Whether each named
test passes is the suite's own business: they run in it.

Three of the exit criteria are held by documents, not code, and the last
tests here hold those documents: the legacy guide stays gone and nothing
current points at it, ADR 0001 stays and says ADR 0009 supersedes it, and
the changelog keeps the breaking change with its manual clean-up.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR_DIR = REPO_ROOT / "docs" / "adr"
ADR_0009_NAME = "0009-removing-local-mode-strict-refusal-protocol-and-sequencing.md"
ADR = ADR_DIR / ADR_0009_NAME
ADR_0001 = ADR_DIR / "0001-local-mode-workspace-identity-and-filesystem-boundary.md"
CHANGELOG = REPO_ROOT / "CHANGELOG.md"

C6 = "### C.6 A.2.4's invariants and the tests that hold them on `main`"
EXIT_CRITERIA = "### D.1 The exit criteria"

# A test named in the record: `tests/test_x.py::test_y`.
_TEST_REF = re.compile(r"`(tests/test_[a-z0-9_]+\.py)::(test_[a-z0-9_]+)`")
# An attestation in C.6 or D.1: a bullet that opens with its subject in bold.
_ATTESTATION = re.compile(r"^- \*\*(?P<subject>.+?)\*\*")
# D.1's subject: the criterion's number in EPIC #383's list, then its words.
_CRITERION = re.compile(r"^E(?P<number>[0-9]+)\. \S")
# D.1's verdict, in italics right after the subject.
_VERDICT = re.compile(r"^- \*\*.+?\*\* \*(?P<verdict>[^*]+)\.\*")
VERDICTS = ("Met", "Met in part", "Open")
# EPIC #383's exit criteria, a list of twelve in its body.
EPIC_CRITERIA = 12


def _section(text: str, heading: str) -> str:
    """The body of the section ``heading`` opens, up to the next heading of its level or above."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
    assert heading in lines, f"no section {heading!r}"
    start = lines.index(heading)
    body = []
    for line in lines[start + 1 :]:
        hashes = len(line) - len(line.lstrip("#"))
        if line.startswith("#") and hashes <= level and line[hashes : hashes + 1] == " ":
            break
        body.append(line)
    return "\n".join(body)


@cache
def _record() -> str:
    return ADR.read_text(encoding="utf-8")


def preserved_invariants(text: str) -> list[str]:
    """The first column of A.2.4's table: each shared invariant LOCAL's removal kept."""
    section = _section(text, "#### A.2.4 Shared safety and its preservation owner")
    rows = [line for line in section.splitlines() if line.startswith("|")]
    assert rows[0].startswith("| Invariant |") and set(rows[1]) <= {"|", "-"}, rows[:2]
    return [row.split("|")[1].strip() for row in rows[2:]]


def _bullets(text: str, heading: str) -> dict[str, tuple[str, list[tuple[str, str]]]]:
    """The bold-opened bullets of ``heading``'s section: each subject, with the
    bullet's opening line and the tests (module, function) the bullet names."""
    found: dict[str, tuple[str, list[tuple[str, str]]]] = {}
    current: str | None = None
    for line in _section(text, heading).splitlines():
        opened = _ATTESTATION.match(line)
        if opened:
            current = opened.group("subject")
            assert current not in found, f"{current!r} is attested twice"
            found[current] = (line, [])
        if current is not None:
            found[current][1].extend(_TEST_REF.findall(line))
    return found


def attestations(text: str) -> dict[str, list[tuple[str, str]]]:
    """C.6's bullets: each invariant, with the tests (module, function) it names."""
    return {invariant: tests for invariant, (_, tests) in _bullets(text, C6).items()}


def exit_criteria(text: str) -> dict[str, tuple[str | None, list[tuple[str, str]]]]:
    """D.1's bullets: each exit criterion, with its verdict (``None`` when it
    has none) and the tests (module, function) it names."""
    found: dict[str, tuple[str | None, list[tuple[str, str]]]] = {}
    for criterion, (opening, tests) in _bullets(text, EXIT_CRITERIA).items():
        verdict = _VERDICT.match(opening)
        found[criterion] = (verdict.group("verdict") if verdict else None, tests)
    return found


@cache
def _test_functions(module: str) -> frozenset[str]:
    """The test functions a test module defines at its top level, read without importing it."""
    tree = ast.parse((REPO_ROOT / module).read_text(encoding="utf-8"))
    return frozenset(
        node.name
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    )


def _named_tests() -> list[tuple[str, str]]:
    """Every test C.6 or D.1 names, once."""
    named = {ref for refs in attestations(_record()).values() for ref in refs}
    named |= {ref for _, refs in exit_criteria(_record()).values() for ref in refs}
    return sorted(named)


def test_every_invariant_a_2_4_lists_is_attested_and_nothing_else():
    """C.6 attests exactly A.2.4's invariants, in its words, each once."""
    listed = preserved_invariants(_record())
    assert len(listed) == len(set(listed)) == 16
    assert list(attestations(_record())) == listed


def test_every_attestation_names_a_test():
    assert all(attestations(_record()).values()), [
        invariant for invariant, tests in attestations(_record()).items() if not tests
    ]


def test_the_exit_audit_takes_each_of_the_epics_criteria_once_in_order():
    """D.1 numbers EPIC #383's exit criteria E1 to E12, in the body's order,
    and takes each once (a second bullet for one is refused by the reader)."""
    numbers = []
    for criterion in exit_criteria(_record()):
        numbered = _CRITERION.match(criterion)
        assert numbered, f"{criterion!r} does not open with its number, E<n>."
        numbers.append(int(numbered.group("number")))
    assert numbers == list(range(1, EPIC_CRITERIA + 1))


def test_every_exit_criterion_has_a_verdict_and_a_met_one_names_a_test():
    """A criterion the audit calls met, or met in part, names a test that
    holds what the repository can show of it; only an open one may name
    none, its evidence being GitHub's (D.4)."""
    for criterion, (verdict, tests) in exit_criteria(_record()).items():
        assert verdict in VERDICTS, f"{criterion!r}: verdict {verdict!r}"
        assert tests or verdict == "Open", f"{criterion!r} is {verdict} and names no test"


@pytest.mark.parametrize("module, function", _named_tests(), ids=lambda value: value)
def test_every_test_an_attestation_names_exists(module: str, function: str):
    """A renamed or deleted test fails here, naming the attestation that names it."""
    assert (REPO_ROOT / module).is_file(), f"{module} does not exist"
    assert function in _test_functions(module), f"{module} defines no {function}"


def test_the_reader_refuses_what_it_should_not_take_for_an_attestation():
    """The parser itself: a section without bullets attests nothing, a
    bullet names only backticked ``tests/...::test_...`` references, and an
    invariant attested twice is refused."""
    heading = C6
    assert attestations(f"{heading}\n\nNo bullets here.\n\n### C.7 Next\n") == {}
    one = (
        f"{heading}\n\n- **Finding text cannot break out of its block**: "
        "`tests/test_findings_fence.py::test_x`, tests/test_engine.py::test_y,\n"
        "  `tests/test_engine.py::test_z`.\n\n"
        "## Appendix D\n- **Not C.6** `tests/test_a.py::test_b`\n"
    )
    assert attestations(one) == {
        "Finding text cannot break out of its block": [
            ("tests/test_findings_fence.py", "test_x"),
            ("tests/test_engine.py", "test_z"),
        ]
    }
    with pytest.raises(AssertionError, match="attested twice"):
        attestations(f"{heading}\n- **A** `tests/test_a.py::test_a`\n- **A** x\n")


def test_the_reader_takes_a_verdict_only_in_italics_after_the_criterion():
    """A verdict is read from the criterion's opening line alone, in italics
    right after it; one in the text below, or in bold, is no verdict."""
    text = (
        f"{EXIT_CRITERIA}\n\n"
        "- **E1. One.** *Met.* Held by `tests/test_a.py::test_a`.\n"
        "- **E2. Two.** *Met in part.* `tests/test_a.py::test_b`;\n"
        "  the rest is open.\n"
        "- **E3. Three.** *Open.* GitHub's.\n"
        "- **E4. Four.** Held, said in passing.\n  *Met.*\n"
        "- **E5. Five.** **Met.**\n"
        "### D.2 Next\n- **E6. Not D.1.** *Met.*\n"
    )
    assert exit_criteria(text) == {
        "E1. One.": ("Met", [("tests/test_a.py", "test_a")]),
        "E2. Two.": ("Met in part", [("tests/test_a.py", "test_b")]),
        "E3. Three.": ("Open", []),
        "E4. Four.": (None, []),
        "E5. Five.": (None, []),
    }


# -- the documents the exit criteria name (E3, E10, E11) ----------------------


def _current_documents() -> list[Path]:
    """Every Markdown file that says how AutoForge is now: C.8's list, without
    the ADRs and the changelog, which are history (ADR 0009 D10, D11)."""
    found = [*REPO_ROOT.glob("*.md"), *(REPO_ROOT / "docs").glob("*.md")]
    found += (REPO_ROOT / "docs" / "agent-guides").glob("*.md")
    found += (REPO_ROOT / "src" / "autoforge").glob("*.md")
    found += (REPO_ROOT / "tests").glob("*.md")
    return sorted(p for p in found if p != CHANGELOG)


# A link to the deleted guide, or to the README section that described
# LOCAL (`#local-mode-no-github`, ADR 0009 D5).
_TO_THE_LEGACY_GUIDE = re.compile(r"(?<![\w-])local-mode(?:\.cn)?\.md|#local-mode\b")


def test_the_legacy_guide_is_gone_and_no_current_document_points_at_it():
    """E3: no fallback documentation or legacy guide. ``docs/local-mode.md``,
    its translation included, went in S2 (#387, ADR 0009 D5); a current
    document that links to it, or to README's LOCAL section, points at what
    is gone. ADR 0001's own file name has ``local-mode-`` in it and is no
    match."""
    assert sorted(p.name for p in (REPO_ROOT / "docs").glob("local-mode*")) == []
    documents = _current_documents()
    assert {REPO_ROOT / "README.md", REPO_ROOT / "docs" / "usage.md"} <= set(documents)
    offenders = [
        f"{path.relative_to(REPO_ROOT)}:{number}"
        for path in documents
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if _TO_THE_LEGACY_GUIDE.search(line)
    ]
    assert offenders == []
    assert _TO_THE_LEGACY_GUIDE.search("see [the guide](docs/local-mode.md)")
    assert _TO_THE_LEGACY_GUIDE.search("(README.md#local-mode-no-github)")
    assert not _TO_THE_LEGACY_GUIDE.search(ADR_0001.name)


def test_adr_0001_is_kept_and_says_adr_0009_supersedes_it():
    """E10: ADR 0001 is retained and marked superseded (D11). Its header names
    ADR 0009 once and the parts it keeps, the ones that still describe
    ``safefs.py``, and those parts are still in it. That the mark came with
    the removal's completion, not before, is history (Appendix D, E10)."""
    text = ADR_0001.read_text(encoding="utf-8")
    header = text.split("\n## ", 1)[0]
    marks = [line for line in header.splitlines() if line.startswith("- **Superseded by:**")]
    assert len(marks) == 1 and f"]({ADR_0009_NAME})" in marks[0], marks
    assert ADR.is_file()
    kept = " ".join(header.split("- **Superseded by:**", 1)[1].split("\n- **", 1)[0].split())
    for part in ("§5.4", "§5.5", "§8.8", "§8.10"):
        assert part in kept, part
    lines = text.splitlines()
    assert "### 5.4 One capability boundary for every controller write" in lines
    assert "### 5.5 Cost is a refusal, not a sample" in lines
    limitations = _section(text, "## 8. Known limitations")
    assert re.search(r"^8\. \*\*Artifacts are published by `link\(2\)`", limitations, re.M)
    assert re.search(r"^10\. \*\*A controller write can be \*observed\*", limitations, re.M)


def test_the_changelog_records_the_breaking_removal_and_its_manual_clean_up():
    """E11: the breaking change is in ``CHANGELOG.md`` with manual clean-up
    guidance (ADR 0009 D10): what is gone, and what a LOCAL run left that
    the operator may archive or remove by hand, with the one directory never
    to remove whole. The entries stay when their heading moves from
    Unreleased to a release."""
    text = CHANGELOG.read_text(encoding="utf-8")
    removed = " ".join(_section(text, "### LOCAL mode is removed (#387)").split())
    assert removed.startswith("**Behaviour change:** LOCAL mode, the GitHub-free workflow, is")
    for words in (
        "`autoforge local` (`init`, `run`, `doctor`), the `local:` configuration key",
        "What a LOCAL run left, which you may archive or remove by hand:",
        "`<git dir>/autoforge/state` by default",
        "`<git dir>/autoforge/sandbox/local`, the sandbox cache",
        "the `local:` block of the configuration file",
        "Never remove `<git common dir>/autoforge` as a whole",
        "`sandbox.mode: preferred` stays an accepted value and now means `required`",
    ):
        assert words in removed, words
    protocol = " ".join(
        _section(
            text, "### The state file is protocol 13, without LOCAL's twelve fields (#388)"
        ).split()
    )
    assert protocol.startswith("**Behaviour change:**")
    assert "An older AutoForge refuses a protocol-13 file." in protocol
