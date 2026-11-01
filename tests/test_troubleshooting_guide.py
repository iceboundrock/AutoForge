"""The troubleshooting guide's recovery scenarios and links (#260).

``docs/troubleshooting.md`` is the operator's symptom-indexed recovery
guide. Its scenario table claims, for each recovery scenario, that a named
test holds the documented behaviour; a test renamed or deleted would leave
that claim pointing at nothing. These tests keep the claim checkable: every
row names at least one test, every named test exists, and every link of the
guide, and every link into it from another document, reaches a file and a
heading that exist. They read files only; what each cited test asserts is
the cited test's own business.
"""

from __future__ import annotations

import ast
import re
from collections import Counter
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GUIDE = ROOT / "docs" / "troubleshooting.md"
BEGIN = "<!-- recovery-scenarios -->"
END = "<!-- /recovery-scenarios -->"
COLUMNS = ("#", "Scenario", "What AutoForge does", "How to recover", "Evidence")
CITATION = re.compile(r"`(tests/test_\w+\.py)::(test_\w+)`")
LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")
FENCE = re.compile(r"^\s*(```|~~~)")
HEADING = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
# Directories whose Markdown is not the repository's documentation.
SKIPPED = {".git", ".venv", "node_modules", ".pytest_cache", ".ruff_cache", ".mypy_cache"}


def _rows() -> list[list[str]]:
    """The scenario table's rows, header and separator excluded, as cell lists."""
    text = GUIDE.read_text(encoding="utf-8")
    assert text.count(BEGIN) == 1 and text.count(END) == 1, "the table's markers"
    table = text.split(BEGIN, 1)[1].split(END, 1)[0].strip().splitlines()
    header, separator, *rows = table
    assert _cells(header) == list(COLUMNS)
    assert set(separator.replace("|", "")) == {"-"}
    return [_cells(row) for row in rows]


def _cells(row: str) -> list[str]:
    assert row.startswith("|") and row.endswith("|"), row
    return [cell.strip() for cell in row[1:-1].split("|")]


@cache
def _test_names(path: Path) -> frozenset[str]:
    """The test functions a module defines, at its top level or in a class."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    return frozenset(
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
    )


def _slug(heading: str) -> str:
    """The anchor GitHub gives a heading: link text kept, punctuation dropped,
    lower case, spaces as hyphens."""
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading)
    return re.sub(r"[^\w\- ]", "", text.strip().lower()).replace(" ", "-")


@cache
def _anchors(path: Path) -> frozenset[str]:
    """Every heading anchor of a Markdown file, duplicates numbered as GitHub does."""
    seen: Counter[str] = Counter()
    anchors = set()
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
            continue
        match = None if fenced else HEADING.match(line)
        if match:
            slug = _slug(match[2])
            anchors.add(slug if not seen[slug] else f"{slug}-{seen[slug]}")
            seen[slug] += 1
    return frozenset(anchors)


def _links(path: Path) -> list[str]:
    """The link targets of a Markdown file, outside fenced code."""
    targets = []
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if FENCE.match(line):
            fenced = not fenced
        elif not fenced:
            targets += LINK.findall(line)
    return targets


def _broken(source: Path, target: str) -> str:
    """Why a relative link of ``source`` reaches nothing; "" when it resolves."""
    file_part, _, anchor = target.partition("#")
    destination = (source.parent / file_part).resolve() if file_part else source
    if not destination.exists():
        return f"{source.relative_to(ROOT)}: {target}: no such file"
    if anchor and destination.suffix == ".md" and anchor not in _anchors(destination):
        return f"{source.relative_to(ROOT)}: {target}: no such heading"
    return ""


def _markdown_files() -> list[Path]:
    return [
        path
        for path in ROOT.rglob("*.md")
        if not SKIPPED.intersection(path.relative_to(ROOT).parts)
    ]


def test_every_scenario_row_names_a_test():
    """A row with no test would be an unsubstantiated recovery claim; the
    guide lists such gaps under Known limitations instead."""
    rows = _rows()
    assert rows, "the scenario table is empty"
    ids = []
    for cells in rows:
        assert len(cells) == len(COLUMNS), cells
        assert all(cells), f"an empty cell in {cells[0]}"
        assert re.fullmatch(r"S\d+", cells[0]), cells[0]
        ids.append(cells[0])
        assert CITATION.search(cells[-1]), f"{cells[0]} names no test"
    assert ids == [f"S{n}" for n in range(1, len(ids) + 1)], "rows numbered in order"


def test_every_test_the_scenarios_name_exists():
    missing = []
    for cells in _rows():
        for module, name in CITATION.findall(cells[-1]):
            path = ROOT / module
            if not path.is_file() or name not in _test_names(path):
                missing.append(f"{cells[0]}: {module}::{name}")
    assert missing == []


def test_the_guides_links_resolve():
    broken = [
        problem
        for target in _links(GUIDE)
        if "://" not in target and not target.startswith("mailto:")
        for problem in [_broken(GUIDE, target)]
        if problem
    ]
    assert broken == []


def test_every_link_into_the_guide_resolves():
    """Other documents link to the guide's sections by anchor; a renamed
    heading would leave them pointing at the top of the page."""
    broken = []
    for source in _markdown_files():
        for target in _links(source):
            file_part = target.partition("#")[0]
            if "://" in target or not file_part:
                continue
            if (source.parent / file_part).resolve() == GUIDE:
                broken += [p for p in [_broken(source, target)] if p]
    assert broken == []


def test_the_slug_matches_githubs_for_the_headings_the_guide_links_to():
    """The anchor rule this module checks links with, on headings of the
    shapes the documents use: code spans, colons, apostrophes, underscores,
    quotes, a dated ADR amendment."""
    assert _slug("BLOCKED: fix the cause, then unblock") == "blocked-fix-the-cause-then-unblock"
    assert _slug("READY_FOR_MERGE: a human merges, unless the gate is open") == (
        "ready_for_merge-a-human-merges-unless-the-gate-is-open"
    )
    assert _slug("`epic status`: one category per child") == "epic-status-one-category-per-child"
    assert _slug("Doctor's Pi rows") == "doctors-pi-rows"
    assert _slug('BLOCKED, FAILED or "State unchanged"') == "blocked-failed-or-state-unchanged"
    assert _slug("4e. Amendment: sandboxed launches (#167, 2026-10-12)") == (
        "4e-amendment-sandboxed-launches-167-2026-10-12"
    )
