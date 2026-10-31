"""ADR 0009 Appendix C.6's attestation of A.2.4 stays true (#389).

LOCAL mode's removal (ADR 0009) kept every shared invariant a LOCAL test
used to protect: Appendix A.2.4 lists those invariants with their owners,
and Appendix C.6, written by S4 (#389), names for each one the tests on
`main` that hold it now. These tests read both lists from the record and
fail when

- C.6 attests an invariant A.2.4 does not list, or leaves one out;
- an attestation names no test;
- a test it names is not a test function of its module any more.

So a test that is renamed or deleted takes the record with it, instead of
leaving it attesting coverage the suite no longer has. Whether each named
test passes is the suite's own business: they run in it.
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
ADR = (
    REPO_ROOT
    / "docs"
    / "adr"
    / "0009-removing-local-mode-strict-refusal-protocol-and-sequencing.md"
)
# A test named in the record: `tests/test_x.py::test_y`.
_TEST_REF = re.compile(r"`(tests/test_[a-z0-9_]+\.py)::(test_[a-z0-9_]+)`")
# An attestation in C.6: a bullet that opens with the invariant in bold.
_ATTESTATION = re.compile(r"^- \*\*(?P<invariant>.+?)\*\*")


def _section(text: str, heading: str) -> str:
    """The body of the section ``heading`` opens, up to the next heading of its level or above."""
    level = len(heading) - len(heading.lstrip("#"))
    lines = text.splitlines()
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


def attestations(text: str) -> dict[str, list[tuple[str, str]]]:
    """C.6's bullets: each invariant, with the tests (module, function) it names."""
    body = _section(text, "### C.6 A.2.4's invariants and the tests that hold them on `main`")
    found: dict[str, list[tuple[str, str]]] = {}
    current: str | None = None
    for line in body.splitlines():
        opened = _ATTESTATION.match(line)
        if opened:
            current = opened.group("invariant")
            assert current not in found, f"{current!r} is attested twice"
            found[current] = []
        if current is not None:
            found[current] += _TEST_REF.findall(line)
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


def test_every_invariant_a_2_4_lists_is_attested_and_nothing_else():
    """C.6 attests exactly A.2.4's invariants, in its words, each once."""
    listed = preserved_invariants(_record())
    assert len(listed) == len(set(listed)) == 16
    assert list(attestations(_record())) == listed


def test_every_attestation_names_a_test():
    assert all(attestations(_record()).values()), [
        invariant for invariant, tests in attestations(_record()).items() if not tests
    ]


@pytest.mark.parametrize(
    "module, function",
    sorted({ref for refs in attestations(_record()).values() for ref in refs}),
    ids=lambda value: value,
)
def test_every_test_an_attestation_names_exists(module: str, function: str):
    """A renamed or deleted test fails here, naming the attestation that names it."""
    assert (REPO_ROOT / module).is_file(), f"{module} does not exist"
    assert function in _test_functions(module), f"{module} defines no {function}"


def test_the_reader_refuses_what_it_should_not_take_for_an_attestation():
    """The parser itself: a section without bullets attests nothing, a
    bullet names only backticked ``tests/...::test_...`` references, and an
    invariant attested twice is refused."""
    heading = "### C.6 A.2.4's invariants and the tests that hold them on `main`"
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
