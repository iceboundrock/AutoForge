"""The EPIC's managed roadmap section: locate, splice, and hold the rest still (#13).

Pure text tests for ``autoforge.roadmap``. The controller-side behaviour
(who writes, what is read back, when the merge counter resets) is in
``test_engine.py``.
"""

from __future__ import annotations

import pytest

from autoforge.roadmap import (
    ROADMAP_END_MARKER,
    ROADMAP_START_MARKER,
    RoadmapError,
    outside_roadmap,
    render_roadmap_block,
    splice_roadmap,
    split_roadmap,
)

START = ROADMAP_START_MARKER
END = ROADMAP_END_MARKER
OPERATOR_TEXT = "# EPIC\n\nSome intro.\n\n- [ ] task one\n- [x] task two\n"


def test_markers_are_html_comments_the_controller_owns():
    assert START == "<!-- ai-controller-roadmap:start -->"
    assert END == "<!-- ai-controller-roadmap:end -->"


def test_split_without_markers_is_no_section():
    split = split_roadmap(OPERATOR_TEXT)
    assert split.section is None and not split.present
    assert split.before == OPERATOR_TEXT and split.after == ""
    assert split.outside == OPERATOR_TEXT and split.body == OPERATOR_TEXT


def test_split_locates_the_section_between_the_marker_lines():
    body = f"{OPERATOR_TEXT}\n{START}\n## Roadmap\n- a\n{END}\n\nTrailing note.\n"
    split = split_roadmap(body)
    assert split.section == "## Roadmap\n- a"
    assert split.before == f"{OPERATOR_TEXT}\n{START}"
    assert split.after == f"{END}\n\nTrailing note.\n"
    assert split.outside == f"{OPERATOR_TEXT}\n{START}{END}\n\nTrailing note.\n"
    assert split.body == body


def test_split_of_an_empty_section():
    assert split_roadmap(f"{START}\n{END}").section == ""
    assert split_roadmap(f"{START}{END}").section == ""


@pytest.mark.parametrize(
    "body",
    [
        f"{START}\nx\n",  # start without end
        f"x\n{END}\n",  # end without start
        f"{START}\na\n{END}\n{START}\nb\n{END}\n",  # two sections
        f"{START}\n{START}\na\n{END}\n",  # repeated start
        f"{START}\na\n{END}\n{END}\n",  # repeated end
        f"{END}\na\n{START}\n",  # end before start
    ],
)
def test_ambiguous_markers_are_refused(body):
    with pytest.raises(RoadmapError):
        split_roadmap(body)
    with pytest.raises(RoadmapError):
        outside_roadmap(body)
    with pytest.raises(RoadmapError):
        splice_roadmap(body, "section")


def test_splice_appends_a_section_after_the_operator_text_unchanged():
    new = splice_roadmap(OPERATOR_TEXT, "## Roadmap\n- a")
    assert new.startswith(OPERATOR_TEXT)  # every original byte, in place
    assert new == f"{OPERATOR_TEXT}\n{START}\n## Roadmap\n- a\n{END}\n"
    assert split_roadmap(new).section == "## Roadmap\n- a"


@pytest.mark.parametrize(
    ("body", "expected_prefix"),
    [
        ("", ""),
        ("no trailing newline", "no trailing newline\n\n"),
        ("one trailing newline\n", "one trailing newline\n\n"),
        ("blank line at the end\n\n", "blank line at the end\n\n"),
    ],
)
def test_splice_append_separates_the_block_by_one_blank_line(body, expected_prefix):
    assert splice_roadmap(body, "s") == f"{expected_prefix}{render_roadmap_block('s')}\n"


def test_splice_replaces_the_section_and_nothing_outside_it():
    body = f"{OPERATOR_TEXT}\n{START}\nold\n{END}\n\nTrailing note.\n"
    new = splice_roadmap(body, "## Roadmap\n- new")
    assert new == f"{OPERATOR_TEXT}\n{START}\n## Roadmap\n- new\n{END}\n\nTrailing note.\n"
    assert outside_roadmap(new) == outside_roadmap(body)
    assert split_roadmap(new).section == "## Roadmap\n- new"


def test_splice_is_idempotent():
    body = f"{OPERATOR_TEXT}\n{START}\nsame\n{END}\n"
    assert splice_roadmap(body, "same") == body
    once = splice_roadmap(OPERATOR_TEXT, "same")
    assert splice_roadmap(once, "same") == once


def test_splice_refuses_a_section_carrying_a_marker():
    for bad in (f"a\n{START}\nb", f"a\n{END}"):
        with pytest.raises(RoadmapError, match="must not contain the roadmap markers"):
            splice_roadmap(OPERATOR_TEXT, bad)
