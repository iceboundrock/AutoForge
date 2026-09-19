"""The EPIC's managed roadmap section: locate, splice, and prove the rest untouched.

The EPIC body is the operator's document. AutoForge owns exactly one part
of it, the section between two marker lines::

    <!-- ai-controller-roadmap:start -->
    ...
    <!-- ai-controller-roadmap:end -->

and nothing else. The UPDATE_EPIC agent never edits the body: it returns
the section's new content in its ``CONTROL_RESULT`` and the controller
performs the edit (``gh issue edit --body-file``), reads the body back, and
requires every byte outside the markers to be identical to what it read
before writing. That read-back is the only evidence the update happened;
the counter of merges awaiting a roadmap update is reset after it and
never before.

This module is pure text logic so the splice and its acceptance rules can
be tested without an engine or a GitHub fake: :func:`split_roadmap` finds
the section (or proves there is none), :func:`splice_roadmap` builds the
body that carries a new one, and :func:`outside_roadmap` is the text the
read-back compares. A body whose markers cannot be read unambiguously (a
marker repeated, an end before a start, one without the other) raises
:class:`RoadmapError`: the controller then blocks rather than guess which
part of the operator's document is its own.
"""

from __future__ import annotations

from dataclasses import dataclass

from .errors import AutoForgeError

ROADMAP_START_MARKER = "<!-- ai-controller-roadmap:start -->"
ROADMAP_END_MARKER = "<!-- ai-controller-roadmap:end -->"


class RoadmapError(AutoForgeError):
    """The EPIC body's managed section cannot be located unambiguously."""


@dataclass(frozen=True)
class RoadmapSplit:
    """An EPIC body cut around its managed section.

    ``body`` is the text as read. ``before`` ends just after the start
    marker (or is the whole body when there is no section), ``section`` is
    the content between the marker lines (``None`` when absent), and
    ``after`` starts at the end marker. ``outside`` is what a read-back after
    the controller's write must find byte-identical.
    """

    body: str
    before: str
    section: str | None
    after: str

    @property
    def present(self) -> bool:
        return self.section is not None

    @property
    def outside(self) -> str:
        """Every byte that is not managed-section content."""
        return self.before + self.after


def _count(body: str, marker: str) -> int:
    return body.count(marker)


def split_roadmap(body: str) -> RoadmapSplit:
    """Locate the managed section of ``body``.

    Exactly one start marker followed by exactly one end marker is a
    section; neither marker at all is "no section yet" (the splice appends
    one). Anything else is ambiguous and raises :class:`RoadmapError`.
    """
    starts = _count(body, ROADMAP_START_MARKER)
    ends = _count(body, ROADMAP_END_MARKER)
    if starts == 0 and ends == 0:
        return RoadmapSplit(body=body, before=body, section=None, after="")
    if starts != 1 or ends != 1:
        raise RoadmapError(
            f"the EPIC body carries {starts} start and {ends} end roadmap marker(s); the "
            f"managed section is exactly one {ROADMAP_START_MARKER!r} followed by exactly one "
            f"{ROADMAP_END_MARKER!r}"
        )
    start = body.index(ROADMAP_START_MARKER)
    end = body.index(ROADMAP_END_MARKER)
    if end < start:
        raise RoadmapError(
            "the EPIC body's roadmap end marker comes before its start marker; the managed "
            "section cannot be located"
        )
    inner_start = start + len(ROADMAP_START_MARKER)
    inner = body[inner_start:end]
    # The section is written as ``START\n<content>\nEND``; the newline that
    # ends the start marker's line and the one that ends the content's last
    # line are structure, not content. A body edited by hand may lack either.
    content = inner
    if content.startswith("\n"):
        content = content[1:]
    if content.endswith("\n"):
        content = content[:-1]
    return RoadmapSplit(body=body, before=body[:inner_start], section=content, after=body[end:])


def outside_roadmap(body: str) -> str:
    """Every byte of ``body`` that is not managed-section content."""
    return split_roadmap(body).outside


def render_roadmap_block(section: str) -> str:
    """The managed section as it is written into a body."""
    return f"{ROADMAP_START_MARKER}\n{section}\n{ROADMAP_END_MARKER}"


def splice_roadmap(body: str, section: str) -> str:
    """``body`` with its managed section replaced by ``section``, or appended.

    Replacing keeps every byte outside the marker lines; appending adds the
    block after the existing text, separated by a blank line, and never
    changes a byte of it. ``section`` must not carry a marker itself (the
    result parser refuses one), so the spliced body always splits back into
    exactly this section.
    """
    if ROADMAP_START_MARKER in section or ROADMAP_END_MARKER in section:
        raise RoadmapError("a roadmap section must not contain the roadmap markers")
    split = split_roadmap(body)
    if split.present:
        # ``before`` ends with the start marker; ``after`` starts with the end
        # marker. The content goes between them on lines of its own.
        return f"{split.before}\n{section}\n{split.after}"
    block = render_roadmap_block(section)
    if not body:
        return block + "\n"
    separator = "" if body.endswith("\n\n") else ("\n" if body.endswith("\n") else "\n\n")
    return f"{body}{separator}{block}\n"


__all__ = [
    "ROADMAP_END_MARKER",
    "ROADMAP_START_MARKER",
    "RoadmapError",
    "RoadmapSplit",
    "outside_roadmap",
    "render_roadmap_block",
    "splice_roadmap",
    "split_roadmap",
]
