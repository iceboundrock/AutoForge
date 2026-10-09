"""CONTROL_RESULT protocol: extraction + strict per-phase validation.

Agent stdout may contain arbitrary logs, but must end with exactly one
machine-readable block::

    <<<CONTROL_RESULT>>>
    { ... JSON object ... }
    <<<END_CONTROL_RESULT>>>

Rules: find ALL complete blocks, use only the LAST one; JSON must be valid;
payload must be an object containing at least ``phase`` and ``status``; the
payload phase must equal the controller's current phase; per-phase required
fields are enforced with typed dataclass models.

The controller never "guesses what the agent meant": a REVIEW result whose
``needs_fix_round`` disagrees with its ``findings`` list, or a FIX
resolution without the evidence its kind requires, is rejected outright. So
is a REVIEW result larger than the controller is willing to persist and
render (``MAX_FINDINGS_PER_REVIEW`` and the per-field character bounds,
finding ids included): it is refused whole, never clipped, so the reviewer
can re-emit it. The FIX payload is bounded the same way
(``MAX_RESOLUTIONS_PER_FIX``, ``MAX_FIX_RATIONALE_CHARS``, the shape of
``commit_sha`` and the length of any URL): every resolution is persisted
whole. A finding's one-line fields (``title``, ``location``) may carry no
control character at all, and its ``required_resolution`` and a FIX
``rationale`` only a newline or a tab (#78): the prompt renderer would
escape or indent anything else, and a value the controller would have to
rewrite before it can show it is refused, not repaired. The same holds for
the block as a whole
(``MAX_CONTROL_RESULT_CHARS``): the accepted payload is persisted whole and
is what the next phase acts on, so its size is checked before it is decoded.

Agent text the controller publishes is held to one content policy (ADR 0004
D8.2, D8.3, D8.5): no controller marker opener of either prefix, no
credential-shaped string (refused by the redactor's pattern class, never
redacted), no closing keyword followed by an issue reference, and no
``@``-mention outside code. :func:`published_text_problem` judges one field,
:func:`published_payload_problem` a whole rendered payload,
:func:`published_markdown_problem` the Markdown several fields compose and
:func:`commit_message_problem` a commit message; each names the subject and
the rule, never the text. An ANALYZE_EXECUTE result carries the PR title
and body the controller publishes (``pr_title``, ``pr_body``), under that
policy and bounded (``MAX_PR_TITLE_CHARS``, ``MAX_PR_BODY_CHARS``), plus
the issue URL and the candidate SHA as cross-checks; it names no PR, branch
or push target (#161). A REPLAN_REEXECUTE result carries the replacement
PR's title and body under the same policy and bounds, the candidate SHA and
the transaction checkpoint's cross-checks, and names no replacement PR,
branch or push target either (#164). A REMOTE REVIEW result carries the prose sections of
the review comment the controller renders and posts (``spec``,
``standards``, ``assessment``, ``observations``, ``verification``,
``summary``), each under that policy and bounded
(``MAX_REVIEW_SECTION_CHARS``, ``MAX_REVIEW_SUMMARY_CHARS``), and so is
every finding field the comment shows; ``round`` and ``reviewed_head_sha``
are cross-checks, and it names no comment (#162). A REMOTE FIX result
carries one resolution per open finding; a deferral either names an issue
the controller handed over or carries the title and body of a new follow-up
issue the controller creates, under that policy and bounded
(``MAX_FOLLOW_UP_TITLE_CHARS``, ``MAX_FOLLOW_UP_BODY_CHARS``), and
``previous_head_sha`` and ``head_sha`` are cross-checks; it names no push
target and creates nothing (#163). An UPDATE_EPIC result is validated
against the schema of the request that launched the agent
(:class:`UpdateEpicRequest`, D4.7): a re-request after publication carries
only what it asks for.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum

from .errors import ConfigurationError, ControlResultError, ControlResultValidationError
from .prompts import CONTROL_CHAR_RE, CONTROL_CHARS
from .redaction import credential_classes
from .roadmap import ROADMAP_END_MARKER, ROADMAP_START_MARKER
from .transitions import Phase, WorkflowMode
from .validation import parse_comment_url, parse_issue_url, parse_pr_url

BEGIN = "<<<CONTROL_RESULT>>>"
END = "<<<END_CONTROL_RESULT>>>"

_BLOCK_RE = re.compile(r"<<<CONTROL_RESULT>>>(.*?)<<<END_CONTROL_RESULT>>>", re.DOTALL)
# A SHA field is the full 40-character object id, as the prompts require and
# as GitHub reports it: every SHA the controller accepts is compared by
# equality with one read from GitHub, so an abbreviated SHA could only pass
# the schema and then fail that comparison with a misleading "mismatch"
# instead of the schema error it is (#19). The same shape ``autoforge.claims``
# reads back from a marker.
_SHA_RE = re.compile(r"^[0-9a-fA-F]{40}$")
# The one finding-id rule: the parser applies it to CONTROL_RESULT ids and
# ``autoforge.claims`` to the ids read back from follow-up markers, so a
# marker can never carry an id the parser would have refused. ``\Z``, not
# ``$``: ``$`` also matches before a trailing newline, and ``"R1-F1\n"`` is
# not a finding id (it would look like one and compare unequal to it).
FINDING_ID_RE = re.compile(r"^R(?P<round>[1-9][0-9]*)-F(?P<n>[1-9][0-9]*)\Z")
_FINDING_ID_RE = FINDING_ID_RE
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

ALLOWED_STATUSES = ("success", "failure", "blocked")
FINDING_CLASSIFICATIONS = ("blocked", "non-blocked", "nit")
FIX_RESOLUTIONS = ("fixed", "follow_up_created", "no_change_with_rationale")
# LOCAL mode has no GitHub, so a finding cannot be deferred to a follow-up
# Issue. "unresolved" is the honest local disposition: the fixer could not
# resolve it and says why. The controller treats it as a remaining finding.
LOCAL_FIX_RESOLUTIONS = ("fixed", "no_change_with_rationale", "unresolved")
MIN_RATIONALE_CHARS = 40

# Bounds on the REVIEW payload the controller accepts. Review output is
# untrusted project data, and every accepted finding is persisted in full in
# ``state.open_findings`` (a full atomic rewrite of ``state.json``) and
# rendered verbatim into the next FIX prompt. Without a controller-owned
# limit a single pathological round decides the size of both. An oversized
# result is *rejected*, never clipped: the findings are the work the FIX
# round has to act on, so clipping them would silently drop work, whereas a
# rejection is correctable (the reviewer re-emits a bounded result through the
# ordinary correction retry). Each bound is at or below the corresponding
# persisted-evidence bound in ``loop_guard`` so an accepted round is always
# retained complete (``tests/test_result_parser.py`` pins that relation).
MAX_FINDINGS_PER_REVIEW = 50
MAX_FINDING_RESOLUTION_CHARS = 2000
MAX_FINDING_TITLE_CHARS = 200
MAX_FINDING_LOCATION_CHARS = 300
# A finding id is ``R<round>-F<n>``; its shape does not bound its length (the
# digit runs are open-ended), and the id is persisted and rendered like every
# other finding field, so it is bounded explicitly. The bound is checked
# before the shape, so an oversized id is never echoed into a message. A FIX
# ``finding_id`` must equal an accepted finding's id, so the same bound
# applies to it at parse time rather than after the coverage check.
MAX_FINDING_ID_CHARS = 32
# Bounds on the FIX payload (#77). Every accepted resolution is persisted
# whole in ``state.last_fix_resolutions`` (after redaction, which can lengthen
# it), and in LOCAL mode an ``unresolved`` rationale is echoed into the
# persisted ``block_reason`` that ``status`` shows. A FIX can never
# legitimately report more resolutions than the controller accepted findings,
# so the count bound *is* the REVIEW's, derived rather than restated, and it
# is checked before any element is parsed. The rationale bound times
# ``redaction.MAX_GROWTH_FACTOR`` is at or below
# ``loop_guard.MAX_REQUIRED_RESOLUTION_CHARS`` (``tests/test_result_parser.py``
# pins that relation), so a persisted rationale is never larger than a
# persisted resolution. Rejected, never clipped, like a REVIEW field.
MAX_RESOLUTIONS_PER_FIX = MAX_FINDINGS_PER_REVIEW
MAX_FIX_RATIONALE_CHARS = 2000
# Bound on any URL field before the ``validation`` parser sees it: that
# parser quotes the value in its error, and the error is echoed into the
# correction prompt and the run log. A real GitHub issue, PR or comment URL
# is far shorter.
MAX_URL_CHARS = 512
# Bound on the whole CONTROL_RESULT block (the raw JSON text between the
# markers, in characters). The accepted payload is written whole to
# ``control-result.json`` and as one ``events.jsonl`` line, and its fields
# drive the next phase, so it is bounded where it is accepted: checked by
# size alone before ``json.loads``, rejected rather than clipped. The bound
# sits above the largest REVIEW the field bounds admit even in the worst
# JSON encoding (every character escaped as ``\uXXXX``, six per character:
# ``tests/test_result_parser.py`` pins that relation), so the field bounds,
# not this one, are what a reviewer is held to. Any stdout the parser sees is
# itself bounded by the executor's capture bound.
MAX_CONTROL_RESULT_CHARS = 1024 * 1024
# Bound on the EPIC's managed roadmap section an UPDATE_EPIC result may
# carry. The controller writes it into the EPIC body itself (between the
# roadmap markers, nothing else), so the section is the one piece of agent
# text that reaches a GitHub issue body verbatim; it is bounded where it is
# accepted and rejected, never clipped, so a truncated roadmap is never
# published. GitHub bounds an issue body at 65536 characters; the section
# leaves the operator's own text room.
MAX_ROADMAP_SECTION_CHARS = 32768
# Bound on an UPDATE_EPIC result's ``progress`` text. The controller posts it
# as the EPIC progress comment and appends its own marker, and the whole
# comment must stay far under GitHub's 65,536-character comment limit.
# Rejected, never clipped, like every other published field.
MAX_PROGRESS_CHARS = 16384
# Bounds on the PR text an ANALYZE_EXECUTE result carries (#161). The
# controller creates the PR with this title and with the body followed by
# its own ``Closes #n`` and implementation marker, so the body leaves room
# for those under GitHub's 65,536-character body limit and the effect
# payload's bound; the title is the effect's one-line title bound. Rejected,
# never clipped, like every other published field.
MAX_PR_TITLE_CHARS = 256
MAX_PR_BODY_CHARS = 60000
# Bounds on the prose sections of a REVIEW result (#162). The controller
# renders the round's review comment from the result: its heading, binding
# line, findings, needs-fix line and marker are its own, and each prose
# section is the reviewer's text under its fixed heading. Rejected, never
# clipped, like every other published field; the rendered comment as a
# whole is checked against GitHub's comment limit too (ADR 0004 D8.6),
# because the findings alone may already exceed it.
MAX_REVIEW_SECTION_CHARS = 6000
MAX_REVIEW_SUMMARY_CHARS = 2000
# The prose sections, in the order the comment renders them, each mapped to
# its bound.
REVIEW_PROSE_SECTIONS: tuple[tuple[str, int], ...] = (
    ("spec", MAX_REVIEW_SECTION_CHARS),
    ("standards", MAX_REVIEW_SECTION_CHARS),
    ("assessment", MAX_REVIEW_SECTION_CHARS),
    ("observations", MAX_REVIEW_SECTION_CHARS),
    ("verification", MAX_REVIEW_SECTION_CHARS),
    ("summary", MAX_REVIEW_SUMMARY_CHARS),
)
# Bounds on a new follow-up issue a FIX result asks for (#163). The
# controller creates the issue with this title and with the body followed by
# its own reference to the PR and finding and the finding's follow-up marker,
# so the body leaves room for those under GitHub's 65,536-character body
# limit and the effect payload's bound; the title is the effect's one-line
# title bound. Rejected, never clipped, like every other published field.
MAX_FOLLOW_UP_TITLE_CHARS = 256
MAX_FOLLOW_UP_BODY_CHARS = 60000
# Bounds on the ``tests`` list: names of what the agent ran, recorded with
# the result and never published.
MAX_TESTS_REPORTED = 50
MAX_TEST_CHARS = 500


class UpdateEpicRequest(StrEnum):
    """Which UPDATE_EPIC result the controller asked for (ADR 0004 D4.7).

    ``FULL`` is the phase's launch: ``progress`` (required),
    ``roadmap_section`` (optional) and ``next_issue_url`` (a required key,
    ``null`` when the EPIC is complete). The others are re-requests after the
    phase's publication is complete: each asks only for the input a later
    controller step rejected, and a field outside its schema is refused.

    - ``SELECTION``: ``next_issue_url`` only.
    - ``SELECTION_WITH_ROADMAP``: ``next_issue_url`` and an optional
      ``roadmap_section``.
    - ``ROADMAP``: ``roadmap_section`` only, required and non-blank.
    """

    FULL = "full"
    SELECTION = "selection"
    SELECTION_WITH_ROADMAP = "selection_with_roadmap"
    ROADMAP = "roadmap"


# The opening every controller marker shares: ``<!--``, any whitespace (none
# included), ``ai-``. ``autoforge.claims`` builds each marker kind's scanner
# pattern on this expression, and the roadmap-section refusal below matches
# it, so what the section may not carry is exactly what the scanner would
# later read. The whitespace run is possessive as in the scanner (the
# expression runs over agent-supplied text; see ``MarkerKind.pattern``).
CONTROLLER_MARKER_OPEN_RE = re.compile(r"<!--\s*+ai-")
# A roadmap section may not carry a controller marker: the roadmap markers
# would split the body into more than one managed section, and any other
# ``<!-- ai-`` marker would plant a durable claim in an open issue that the
# controller later scans and trusts (``autoforge.claims``). The refusal uses
# the scanner's own opening, so ``<!--ai-`` and ``<!--\nai-`` are refused too.
_CONTROLLER_MARKER_PREFIX = "<!-- ai-"
# The opening of every marker the controller writes or later trusts: its own
# ``<!-- ai-`` claims (:data:`CONTROLLER_MARKER_OPEN_RE`) and the replan
# transaction's ``<!-- autoforge-`` markers (``autoforge.replan_txn``).
# Agent text may carry neither (ADR 0004 D8.2). Case-insensitive, unlike the
# scanner's own opening: a refusal wider than the scanner fails closed.
# Possessive like it, for the same reason.
AGENT_MARKER_OPEN_RE = re.compile(r"<!--\s*+(?:ai|autoforge)-", re.IGNORECASE)


def extract_last_block(stdout: str) -> str:
    """Return the raw JSON text of the last complete CONTROL_RESULT block."""
    matches = _BLOCK_RE.findall(stdout or "")
    if not matches:
        if BEGIN in (stdout or ""):
            raise ControlResultError(
                "found '<<<CONTROL_RESULT>>>' but no closing "
                "'<<<END_CONTROL_RESULT>>>' — incomplete block"
            )
        raise ControlResultError(
            "no CONTROL_RESULT block found in agent stdout "
            "(expected '<<<CONTROL_RESULT>>>{...}<<<END_CONTROL_RESULT>>>')"
        )
    return matches[-1].strip()


def parse_control_result(
    stdout: str,
    expected_phase: Phase,
    mode: WorkflowMode = WorkflowMode.REMOTE,
    *,
    update_epic_request: UpdateEpicRequest = UpdateEpicRequest.FULL,
) -> dict:
    """Extract + JSON-parse + validate the CONTROL_RESULT payload.

    Returns the payload dict. Raises ControlResultError on extraction/JSON
    problems and ControlResultValidationError on schema/phase problems.
    ``mode`` selects the per-phase schema: a LOCAL phase reports semantic
    facts (a summary, a workspace fingerprint, finding resolutions) and never
    a PR URL, a comment URL or a follow-up Issue. ``update_epic_request``
    selects the UPDATE_EPIC schema (:class:`UpdateEpicRequest`).
    """
    raw = extract_last_block(stdout)
    if len(raw) > MAX_CONTROL_RESULT_CHARS:
        raise ControlResultValidationError(
            f"CONTROL_RESULT block is {len(raw)} characters and the controller accepts at "
            f"most {MAX_CONTROL_RESULT_CHARS}. Keep the block to the fields the phase "
            "requires and re-emit it."
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ControlResultError(f"CONTROL_RESULT block is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise ControlResultValidationError(
            f"CONTROL_RESULT must be a JSON object, got {type(payload).__name__}"
        )
    for key in ("phase", "status"):
        if key not in payload:
            raise ControlResultValidationError(f"CONTROL_RESULT missing required field {key!r}")
    if payload["phase"] != expected_phase.value:
        raise ControlResultValidationError(
            f"phase mismatch: controller is in {expected_phase.value}, "
            f"but CONTROL_RESULT says {payload['phase']!r}"
        )
    if payload["status"] not in ALLOWED_STATUSES:
        raise ControlResultValidationError(
            f"unknown status {payload['status']!r} (expected one of {ALLOWED_STATUSES})"
        )
    if payload["status"] == "success":
        validate_for_phase(expected_phase, payload, mode, update_epic_request=update_epic_request)
    else:
        msg = payload.get("message")
        if not isinstance(msg, str) or not msg.strip():
            raise ControlResultValidationError(
                f"status {payload['status']!r} requires a non-empty 'message' explaining why"
            )
    return payload


# -- helpers --------------------------------------------------------------
def _req(payload: dict, key: str, phase: str):
    if key not in payload or payload[key] in (None, ""):
        raise ControlResultValidationError(
            f"CONTROL_RESULT for {phase} missing required field {key!r}"
        )
    return payload[key]


def _req_str(payload: dict, key: str, phase: str) -> str:
    v = _req(payload, key, phase)
    if not isinstance(v, str):
        raise ControlResultValidationError(f"{phase}: field {key!r} must be a string")
    # ``_req`` rejects "" before stripping; a whitespace-only value is just as
    # absent (a blank required_resolution demands nothing) and is rejected
    # with the same message rather than becoming an empty required field.
    stripped = v.strip()
    if not stripped:
        raise ControlResultValidationError(
            f"CONTROL_RESULT for {phase} missing required field {key!r}"
        )
    return stripped


def _opt_str(payload: dict, key: str, phase: str) -> str:
    """An optional free-text field: absent, JSON ``null``, or a string.

    Never ``str(...)``. Coercion made a number, a list or an object into a
    *successful* result whose text the controller then persisted and rendered
    back into a prompt, so a schema violation arrived as content instead of as
    a rejection. ``null`` is JSON's other spelling of "absent" and is treated
    as such (the same rule :func:`_opt_str_list` already applies); anything
    else is the protocol violation it looks like.
    """
    raw = payload.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ControlResultValidationError(
            f"{phase}: optional field {key!r} must be a string when present, got "
            f"{type(raw).__name__}"
        )
    return raw.strip()


def _checked_sha(v: str, key: str, phase: str) -> str:
    """``v`` as a lower-cased full git SHA, or a rejection that quotes it
    only when it is no longer than a SHA: an oversized value is reported by
    its length, never echoed into the correction prompt or the run log."""
    if not _SHA_RE.match(v):
        shown = repr(v) if len(v) <= 40 else f"{len(v)} characters"
        raise ControlResultValidationError(
            f"{phase}: field {key!r} must be a full git SHA (exactly 40 hex chars, as "
            f"`git rev-parse HEAD` and `gh pr view --json headRefOid` report it), got {shown}"
        )
    return v.lower()


def _req_sha(payload: dict, key: str, phase: str) -> str:
    return _checked_sha(_req_str(payload, key, phase), key, phase)


def _opt_sha(payload: dict, key: str, phase: str) -> str:
    """An optional SHA: absent or ``null`` is ``""``; a present value must be a SHA."""
    v = _opt_str(payload, key, phase)
    return _checked_sha(v, key, phase) if v else ""


def _checked_url(v: str, key: str, phase: str, kind: str) -> str:
    """``v`` validated as a GitHub ``kind`` URL, bounded first.

    The ``validation`` parser quotes the value in its error, and that error
    reaches the correction prompt and the run log, so the length is checked
    before the parser sees the text.
    """
    if len(v) > MAX_URL_CHARS:
        raise ControlResultValidationError(
            f"{phase}: field {key!r} is {len(v)} characters; a GitHub {kind} URL is at most "
            f"{MAX_URL_CHARS}. Re-emit the CONTROL_RESULT with the real URL."
        )
    parser = {"issue": parse_issue_url, "pr": parse_pr_url, "comment": parse_comment_url}[kind]
    try:
        parser(v)
    except ConfigurationError as exc:
        raise ControlResultValidationError(
            f"{phase}: field {key!r} must be a GitHub {kind} URL: {exc}"
        ) from exc
    return v


def _req_url(payload: dict, key: str, phase: str, kind: str) -> str:
    return _checked_url(_req_str(payload, key, phase), key, phase, kind)


def _opt_url(payload: dict, key: str, phase: str, kind: str) -> str:
    """An optional URL: absent or ``null`` is ``""``; a present value must parse."""
    v = _opt_str(payload, key, phase)
    return _checked_url(v, key, phase, kind) if v else ""


def _req_bool(payload: dict, key: str, phase: str) -> bool:
    if key not in payload:
        raise ControlResultValidationError(
            f"CONTROL_RESULT for {phase} missing required field {key!r}"
        )
    v = payload[key]
    if not isinstance(v, bool):
        raise ControlResultValidationError(f"{phase}: field {key!r} must be a boolean")
    return v


# -- published-content policy (ADR 0004 D8.2, D8.3, D8.5) ------------------
# Every rule refuses; none rewrites. Where a rule approximates how GitHub
# reads Markdown, the approximation errs toward refusing: a refusal costs a
# correction, a miss publishes. A message names the subject and the rule,
# and at most an index, never the text.

# A closing keyword followed by an issue reference: ``#n``, ``owner/repo#n``,
# an issue or pull URL, or ``GH-n``. Any case, an optional ``:`` after the
# keyword. Matched anywhere, code spans included: GitHub's own reading of a
# commit message has no code spans, and a wider refusal fails closed. Every
# quantifier is possessive and each candidate starts at a keyword, so a
# candidate scans only the run that follows its own keyword.
_REF_NAME = r"[A-Za-z0-9_.-]++"
_CLOSING_REFERENCE_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)(?::\s*+|\s++)(?:"
    rf"https?://(?:www\.)?github\.com/(?P<url_repo>{_REF_NAME}/{_REF_NAME})"
    r"/(?P<url_kind>issues|pull)/(?P<url_number>[0-9]++)"
    rf"|(?P<repo>{_REF_NAME}/{_REF_NAME})#(?P<repo_number>[0-9]++)"
    r"|#(?P<number>[0-9]++)"
    r"|gh-(?P<gh_number>[0-9]++))",
    re.IGNORECASE,
)
# An ``@`` that starts a GitHub mention: not after an ASCII word character
# (so ``a@b.com`` is an address, not a mention; a non-ASCII letter before
# the ``@`` counts as punctuation, which fails closed), followed by a name
# character. ``@org/team`` starts the same way.
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_])@[A-Za-z0-9]")
# A URL in UPDATE_EPIC ``progress`` (#160: the agent's progress text carries
# no URL; the controller's marker names the issue and the PR). A scheme
# separator ``://``, or a ``www.`` host not inside a word: the two forms
# GitHub autolinks, the first widened to any scheme so the rule errs toward
# refusing. Matched anywhere, code included, and in ``progress`` only:
# ``roadmap_section`` may link the EPIC's PRs.
_PROGRESS_URL_RE = re.compile(r"://|(?<![A-Za-z0-9])www\.", re.IGNORECASE)
# Raw HTML (a tag, a comment, an autolink): GitHub reads backticks and fences
# inside an HTML block or tag as text, so wherever raw HTML sits outside code
# the code exemption for mentions is not trusted at all.
_RAW_HTML_RE = re.compile(r"<[A-Za-z/!?]")
# CommonMark's line endings, and nothing else: ``str.splitlines`` would also
# break at characters GitHub reads as text, and so find fences GitHub does
# not.
_LINE_BREAK_RE = re.compile(r"\r\n|\r|\n")
# A fence opens at column 0 only. CommonMark allows three spaces of indent,
# but an indented ``` may sit inside a list item or block quote, where it is
# not the top-level fence it looks like; at column 0 it is one (or it is in
# an HTML block, which the raw-HTML rule covers). A backtick fence's info
# string has no backtick.
_FENCE_OPEN_RE = re.compile(r"(`{3,}+|~{3,}+)(.*)")
_FENCE_CLOSE_RE = re.compile(r" {0,3}(`{3,}+|~{3,}+)[ \t]*+\Z")
_BACKTICK_RUN_RE = re.compile(r"`++")


def _lines(text: str) -> Iterator[tuple[int, str]]:
    """``(offset, line)`` for each line of ``text``, without its line ending."""
    start = 0
    for m in _LINE_BREAK_RE.finditer(text):
        yield start, text[start : m.start()]
        start = m.end()
    yield start, text[start:]


def _line_code_spans(line: str) -> tuple[list[tuple[int, int]], bool, bool]:
    """The inline code spans of one line, by CommonMark's backtick matching.

    A run of ``n`` backticks opens a span closed by the next run of exactly
    ``n``; a run with no such closer is literal. Returns the spans' content
    ranges, whether every run was paired (``balanced``: nothing can carry
    into the next line) and whether the spans are ``trusted``. They are not
    when an opener is escaped (``\\```), which CommonMark reads differently,
    or a span holds a ``|``, which splits a GFM table cell before code spans
    are parsed. Linear: the next run of each length is found through a
    queue per length, each index dequeued once.
    """
    runs = [(m.start(), m.end()) for m in _BACKTICK_RUN_RE.finditer(line)]
    by_length: dict[int, deque[int]] = {}
    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, deque()).append(index)
    spans: list[tuple[int, int]] = []
    balanced = trusted = True
    k = 0
    while k < len(runs):
        start, end = runs[k]
        if start and line[start - 1] == "\\":
            balanced = trusted = False
        later = by_length[end - start]
        while later and later[0] <= k:
            later.popleft()
        if not later:
            balanced = False
            k += 1
            continue
        closer = later.popleft()
        content_end = runs[closer][0]
        if line.find("|", end, content_end) != -1:
            trusted = False
        spans.append((end, content_end))
        k = closer + 1
    return spans, balanced, trusted


def _code_ranges(text: str) -> list[tuple[int, int]]:
    """Ascending, disjoint ranges of ``text`` that are certainly code.

    Fenced blocks (``` or ~~~, at least three, closed by the same character
    at least as long; an unclosed fence runs to the end) and inline code
    spans. Spans are paired per line, and a line's spans count only when
    they are trusted and no earlier line of the same paragraph run (lines
    since the last blank line or fence) left a backtick unpaired: such a
    backtick may pair across the line break and shift every span after it.
    A multi-line span is therefore never exempt; it fails closed.
    """
    ranges: list[tuple[int, int]] = []
    fence: tuple[str, int] | None = None
    carried = False
    for start, line in _lines(text):
        if fence is not None:
            m = _FENCE_CLOSE_RE.match(line)
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]:
                fence = None
            elif line:
                ranges.append((start, start + len(line)))
            continue
        m = _FENCE_OPEN_RE.match(line)
        if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
            fence = (m.group(1)[0], len(m.group(1)))
            carried = False
            continue
        if not line.strip(" \t"):
            carried = False
            continue
        spans, balanced, trusted = _line_code_spans(line)
        if trusted and not carried:
            ranges.extend((start + s, start + e) for s, e in spans)
        carried = carried or not balanced
    return ranges


def _outside(ranges: list[tuple[int, int]], positions: Iterable[int]) -> Iterator[int]:
    """The ascending ``positions`` that no range of ascending, disjoint ``ranges`` holds."""
    i = 0
    for position in positions:
        while i < len(ranges) and ranges[i][1] <= position:
            i += 1
        if i < len(ranges) and ranges[i][0] <= position:
            continue
        yield position


def _exposed_mention(text: str) -> tuple[int, int | None] | None:
    """The first ``@``-mention of ``text`` GitHub may read as one, if any.

    ``(index, None)`` for a mention outside every code range; ``(index,
    html)`` for the first mention, even one in code, when raw HTML stands
    outside code at ``html``, where the code exemption is not trusted.
    """
    mentions = [m.start() for m in _MENTION_RE.finditer(text)]
    if not mentions:
        return None
    ranges = _code_ranges(text)
    outside = next(_outside(ranges, mentions), None)
    raw_html = next(_outside(ranges, (m.start() for m in _RAW_HTML_RE.finditer(text))), None)
    if raw_html is not None and outside != mentions[0]:
        return mentions[0], raw_html
    if outside is None:
        return None
    return outside, None


def _mention_problem(subject: str, text: str) -> str | None:
    """The refusal of the first ``@``-mention outside code in ``text``, if any."""
    exposed = _exposed_mention(text)
    if exposed is None:
        return None
    index, raw_html = exposed
    if raw_html is not None:
        return (
            f"{subject!r} contains an @-mention at index {index} and raw HTML (a tag, "
            f"comment or autolink) outside code at index {raw_html}; GitHub may read code "
            "near raw HTML as text, so the code exemption does not apply. Remove the HTML or "
            "the mention and re-emit the CONTROL_RESULT."
        )
    return (
        f"{subject!r} contains an @-mention at index {index} outside a code span or fenced "
        "block; GitHub would notify that user or team. Put such tokens in code spans "
        "(`@name`) and re-emit the CONTROL_RESULT."
    )


def published_text_problem(subject: str, text: str) -> str | None:
    """Why agent text ``subject`` may not be published as is, or ``None``.

    The rules, in order (ADR 0004 D8.2, D8.3): a controller marker opener of
    either prefix (:data:`AGENT_MARKER_OPEN_RE`); a credential-shaped string,
    named by its redaction pattern class (refused, never redacted); a closing
    keyword followed by an issue reference, anywhere; an ``@``-mention
    outside a code span or fenced block. The answer is one line naming
    ``subject`` and the rule, never quoting ``text``. Roughly linear in the
    length of ``text``.
    """
    m = AGENT_MARKER_OPEN_RE.search(text)
    if m is not None:
        return (
            f"{subject!r} contains a controller marker opener at index {m.start()} ('<!--' "
            "then 'ai-' or 'autoforge-', in any case or spacing); only the controller writes "
            "markers. Remove it and re-emit the CONTROL_RESULT."
        )
    classes = credential_classes(text)
    if classes:
        return (
            f"{subject!r} contains a credential-shaped string (pattern class: "
            f"{', '.join(classes)}); published text is refused, never redacted. Remove it "
            "and re-emit the CONTROL_RESULT."
        )
    m = _CLOSING_REFERENCE_RE.search(text)
    if m is not None:
        return (
            f"{subject!r} contains a closing keyword followed by an issue reference at index "
            f"{m.start()} ('close', 'fix' or 'resolve' in any form, then '#n', 'owner/repo#n' "
            "or an issue URL), even inside code; GitHub closes issues named that way, and the "
            "controller links the run's own issue itself. Reword it and re-emit the "
            "CONTROL_RESULT."
        )
    return _mention_problem(subject, text)


def published_payload_problem(subject: str, payload: str) -> str | None:
    """Why the rendered ``payload`` ``subject`` may not be published, or ``None``.

    Judges the credential rule alone, on the whole payload (ADR 0004 D8.3):
    fields that pass one by one do not make a payload that passes, because a
    pattern can span a field's end and the text rendered after it (a field
    ending in ``GITHUB_TOKEN=`` takes the next rendered word as its value).
    Names the payload and the pattern classes, never the text.
    """
    classes = credential_classes(payload)
    if not classes:
        return None
    return (
        f"the rendered {subject!r} contains a credential-shaped string (pattern class: "
        f"{', '.join(classes)}), although each field may pass alone: a field's end can join "
        "the text rendered after it. Published text is refused, never redacted; change the "
        "fields so that no credential shape remains and re-emit the CONTROL_RESULT."
    )


def published_markdown_problem(subject: str, markdown: str) -> str | None:
    """Why the Markdown ``subject`` composed of several fields may not be published, or ``None``.

    Judges the mention rule on the composition (ADR 0004 D8.2): fields that
    pass one by one do not make Markdown that passes, because a field's
    unclosed fence, unpaired backtick or raw HTML can change how GitHub
    reads the text rendered after it (a fence one field leaves open is
    closed by another's, and the mention that followed that fence is no
    longer code). ``markdown`` is everything the controller renders before
    its own marker, the one raw HTML it writes. Names the payload and an
    index, never the text.
    """
    exposed = _exposed_mention(markdown)
    if exposed is None:
        return None
    index, raw_html = exposed
    where = (
        f"and raw HTML (a tag, comment or autolink) outside code at index {raw_html}, near "
        "which GitHub may read code as text"
        if raw_html is not None
        else "outside a code span or fenced block"
    )
    return (
        f"the rendered {subject!r} contains an @-mention at index {index} {where}, although "
        "each field may pass alone: an unclosed fence, an unpaired backtick or raw HTML in one "
        "field changes how GitHub reads the fields rendered after it. Close every code span and "
        "fenced block in the field that opens it, drop the raw HTML or the mention, and "
        "re-emit the CONTROL_RESULT."
    )


def markdown_code_span(text: str) -> str:
    """``text`` as one Markdown code span, whatever backticks it holds.

    The delimiter is one backtick longer than the longest run inside, and a
    space pads a value that starts or ends with a backtick, or that starts
    and ends with a space (CommonMark strips one such pair), so the span
    shows ``text`` exactly. One-line text only; an empty value is ``""``.
    """
    if not text:
        return ""
    longest = max((len(run) for run in re.findall(r"`+", text)), default=0)
    fence = "`" * (longest + 1)
    pad = " " if text[0] == "`" or text[-1] == "`" or (text[0] == " " and text[-1] == " ") else ""
    return f"{fence}{pad}{text}{pad}{fence}"


def _names_issue(m: re.Match[str], repository: str, issue_number: int) -> bool:
    """Whether closing reference ``m`` names issue ``issue_number`` of ``repository``.

    Repository names compare case-insensitively, as GitHub treats them; the
    number compares as digits, so a padded ``#07`` is another reference and
    fails closed.
    """
    number = str(issue_number)
    repo = repository.strip().lower()
    if m.group("url_number") is not None:
        return (
            m.group("url_kind").lower() == "issues"
            and m.group("url_repo").lower() == repo
            and m.group("url_number") == number
        )
    if m.group("repo_number") is not None:
        return m.group("repo").lower() == repo and m.group("repo_number") == number
    return (m.group("number") or m.group("gh_number")) == number


def commit_message_problem(message: str, *, repository: str, issue_number: int) -> str | None:
    """Why commit ``message`` may not be published, or ``None`` (ADR 0004 D8.5).

    Refuses a credential-shaped string, and a closing keyword that names any
    issue other than the run's own: ``#<issue_number>``,
    ``<repository>#<issue_number>`` (``owner/repo``, any case) or that
    issue's URL. GitHub closes every issue named that way once the commit
    reaches the default branch. Never quotes the message.
    """
    classes = credential_classes(message)
    if classes:
        return (
            f"commit message contains a credential-shaped string (pattern class: "
            f"{', '.join(classes)}); published text is refused, never redacted. Rewrite the "
            "commit without it."
        )
    for m in _CLOSING_REFERENCE_RE.finditer(message):
        if not _names_issue(m, repository, issue_number):
            return (
                "commit message names an issue other than this run's own "
                f"#{issue_number} with a closing keyword at index {m.start()}; GitHub would "
                "close that issue when the commit reaches the default branch. Reword the "
                f"commit message so that a closing keyword names #{issue_number} only."
            )
    return None


# -- typed per-phase models -----------------------------------------------
@dataclass
class AnalyzeExecuteResult:
    """What an implementation agent reports; the controller publishes it (#161).

    ``issue_url`` and ``head_sha`` are cross-checks against the run's issue
    and the worktree's ``HEAD``, which the controller reads itself; neither
    is a target. ``pr_title`` and ``pr_body`` are the agent's text for the PR
    the controller creates: bounded, held to the published-content policy,
    and never carrying a marker (the controller appends ``Closes #n`` and
    the marker itself). ``tests`` names what the agent ran.
    """

    issue_url: str
    head_sha: str
    pr_title: str
    pr_body: str
    tests: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, p: dict) -> AnalyzeExecuteResult:
        ph = "ANALYZE_EXECUTE"
        issue_url = _req_url(p, "issue_url", ph, "issue")
        head_sha = _req_sha(p, "head_sha", ph)
        title = validate_pr_title(_req_str(p, "pr_title", ph))
        body = validate_pr_body(_req_str(p, "pr_body", ph))
        tests = _opt_str_list(p, "tests", ph)
        if len(tests) > MAX_TESTS_REPORTED:
            raise ControlResultValidationError(
                f"{ph}: field 'tests' lists {len(tests)} entries; the controller accepts at "
                f"most {MAX_TESTS_REPORTED}. Summarise them and re-emit the CONTROL_RESULT."
            )
        for test in tests:
            _one_line(_bounded(test, ph, "result", "tests", MAX_TEST_CHARS), ph, "result", "tests")
        return cls(
            issue_url=issue_url,
            head_sha=head_sha,
            pr_title=title,
            pr_body=body,
            tests=tests,
        )


def _published_field(text: str, key: str, phase: str) -> str:
    """Refuse ``phase``'s field ``key`` under the published-content policy."""
    problem = published_text_problem(key, text)
    if problem is not None:
        raise ControlResultValidationError(f"{phase}: field {problem}")
    return text


def _pr_text(
    text: str, key: str, limit: int, lines: Callable[[str, str, str, str], str], ph: str
) -> str:
    if not isinstance(text, str):
        raise ControlResultValidationError(f"{ph}: field {key!r} must be a string")
    if not text.strip():
        raise ControlResultValidationError(
            f"CONTROL_RESULT for {ph} missing required field {key!r}"
        )
    text = _bounded(text, ph, "result", key, limit)
    return _published_field(lines(text, ph, "result", key), key, ph)


def validate_pr_title(text: str, *, phase: str = "ANALYZE_EXECUTE") -> str:
    """``text`` as a ``pr_title`` of ``phase``, or a rejection.

    Non-blank, at most :data:`MAX_PR_TITLE_CHARS`, one line of printable
    text, and publishable (:func:`published_text_problem`): the controller
    creates the PR with it, the implementation PR of ANALYZE_EXECUTE (#161)
    or the replacement PR of REPLAN_REEXECUTE (#164). The checks
    :meth:`AnalyzeExecuteResult.from_payload` and
    :meth:`ReplanReexecuteResult.from_payload` apply, with the same messages,
    so a persisted K2 or K7 title can be re-validated under the parser's
    rules.
    """
    return _pr_text(text, "pr_title", MAX_PR_TITLE_CHARS, _one_line, phase)


def validate_pr_body(text: str, *, phase: str = "ANALYZE_EXECUTE") -> str:
    """``text`` as a ``pr_body`` of ``phase``, or a rejection.

    Non-blank, at most :data:`MAX_PR_BODY_CHARS`, multi-line text with no
    other control character, and publishable (:func:`published_text_problem`):
    the agent's part of the PR body, before the controller's ``Closes #n``
    and markers. The checks :meth:`AnalyzeExecuteResult.from_payload` and
    :meth:`ReplanReexecuteResult.from_payload` apply, with the same messages,
    so the agent's part of a persisted K2 or K7 body can be re-validated
    under the parser's rules.
    """
    return _pr_text(text, "pr_body", MAX_PR_BODY_CHARS, _multi_line, phase)


# Control characters a *multi-line* text field may still carry: a newline
# structures a resolution and a tab is ordinary indentation; every other
# member of the class the prompt renderer escapes is refused (#78).
_MULTI_LINE_CONTROL_RE = re.compile(rf"(?![\n\t])[{CONTROL_CHARS}]")


def _one_line(text: str, phase: str, subject: str, key: str) -> str:
    """Reject a one-line text field of ``subject`` carrying a control character.

    The class is ``prompts.CONTROL_CHAR_RE``, the one ``escape_inline`` would
    otherwise escape when the field is rendered on one line of a prompt: a
    value the controller would have to rewrite before it can show it is not
    accepted. The message names the code point and its index, never the
    text. Length is checked first (``_bounded``), so the index is into a
    value of accepted size.
    """
    m = CONTROL_CHAR_RE.search(text)
    if m is not None:
        raise ControlResultValidationError(
            f"{phase}: {subject} field {key!r} contains a control character "
            f"(U+{ord(m.group(0)):04X} at index {m.start()}); keep it to one line of "
            "printable text and re-emit the CONTROL_RESULT."
        )
    return text


def _multi_line(text: str, phase: str, subject: str, key: str) -> str:
    """Reject a multi-line text field of ``subject`` carrying a control
    character other than a newline or a tab. Same message discipline as
    :func:`_one_line`."""
    m = _MULTI_LINE_CONTROL_RE.search(text)
    if m is not None:
        raise ControlResultValidationError(
            f"{phase}: {subject} field {key!r} contains a control character "
            f"(U+{ord(m.group(0)):04X} at index {m.start()}); only newlines and tabs are "
            f"accepted inside {key!r}. Remove it and re-emit the CONTROL_RESULT."
        )
    return text


def _bounded(text: str, phase: str, subject: str, key: str, limit: int) -> str:
    """Reject a text field of ``subject`` longer than ``limit`` characters.

    The message reports the size, never the text: the oversized value is the
    thing being refused, and the error is echoed into the correction prompt
    and the run log.
    """
    if len(text) > limit:
        raise ControlResultValidationError(
            f"{phase}: {subject} field {key!r} is {len(text)} characters; the controller "
            f"accepts at most {limit}. Shorten it and re-emit the CONTROL_RESULT."
        )
    return text


def _raw_resolutions(p: dict) -> list:
    """The ``resolutions`` list of a FIX result, count-checked before any
    element is parsed (both modes)."""
    raw = p.get("resolutions")
    if not isinstance(raw, list):
        raise ControlResultValidationError("'resolutions' must be a list (one per finding)")
    if len(raw) > MAX_RESOLUTIONS_PER_FIX:
        raise ControlResultValidationError(
            f"FIX: {len(raw)} resolutions reported; the controller accepts at most "
            f"{MAX_RESOLUTIONS_PER_FIX}, one per open finding. Report exactly one resolution "
            "per finding id listed in the prompt and re-emit the CONTROL_RESULT."
        )
    return raw


def _finding_id(payload: dict, key: str, phase: str, round: int | None = None) -> str:
    """The bounded, well-formed finding id at ``payload[key]``.

    Length is checked first: the shape and round checks quote the id in their
    messages, and those messages reach the correction prompt and the run log.
    With ``round`` given (REVIEW), the id must belong to that round.
    """
    fid = _req_str(payload, key, phase)
    if len(fid) > MAX_FINDING_ID_CHARS:
        raise ControlResultValidationError(
            f"{phase}: field {key!r} is {len(fid)} characters; a finding id is R<round>-F<n> "
            f"and the controller accepts at most {MAX_FINDING_ID_CHARS}. Re-emit the "
            "CONTROL_RESULT with well-formed ids."
        )
    m = _FINDING_ID_RE.match(fid)
    if not m:
        if round is None:
            raise ControlResultValidationError(f"{phase}: invalid {key} {fid!r}")
        raise ControlResultValidationError(
            f"{phase}: finding id {fid!r} must look like R<round>-F<n> (e.g. R{round}-F1)"
        )
    if round is not None and int(m.group("round")) != round:
        raise ControlResultValidationError(
            f"{phase}: finding id {fid!r} does not belong to review round {round}"
        )
    return fid


def _parse_findings(payload: dict, round: int, needs_fix: bool) -> list[Finding]:
    """The bounded, validated findings list of a REVIEW result (any mode).

    The count is checked before any element is parsed, so an oversized list is
    refused without the controller doing per-element work on it, and the
    review invariant (``needs_fix_round == (findings > 0)``) is enforced here
    so REMOTE and LOCAL reviews cannot drift apart.
    """
    raw_findings = payload.get("findings")
    if not isinstance(raw_findings, list):
        raise ControlResultValidationError("'findings' must be a list (empty when clean)")
    if len(raw_findings) > MAX_FINDINGS_PER_REVIEW:
        raise ControlResultValidationError(
            f"REVIEW: {len(raw_findings)} findings reported; the controller accepts at most "
            f"{MAX_FINDINGS_PER_REVIEW} per review round. Keep the actionable findings, move "
            "non-actionable remarks to observations, and re-emit the CONTROL_RESULT."
        )
    findings = [Finding.from_payload(f, round, i) for i, f in enumerate(raw_findings)]
    ids = [f.id for f in findings]
    if len(set(ids)) != len(ids):
        raise ControlResultValidationError(f"duplicate finding ids: {ids}")
    # Controller invariant (§14): the boolean must agree with the list.
    if needs_fix != (len(findings) > 0):
        raise ControlResultValidationError(
            f"invalid REVIEW result: needs_fix_round={needs_fix} but "
            f"{len(findings)} finding(s) reported — the two must agree "
            "(any finding, even a nit, requires a fix round; observations are not findings)"
        )
    return findings


@dataclass
class Finding:
    id: str
    classification: str
    required_resolution: str
    title: str = ""
    location: str = ""

    @classmethod
    def from_payload(cls, raw: object, round: int, index: int) -> Finding:
        ph = "REVIEW"
        if not isinstance(raw, dict):
            raise ControlResultValidationError(
                f"{ph}: findings[{index}] must be an object with id/classification/"
                "required_resolution"
            )
        fid = _finding_id(raw, "id", ph, round)
        subject = f"finding {fid}"
        cls_ = _req_str(raw, "classification", ph)
        if cls_ not in FINDING_CLASSIFICATIONS:
            raise ControlResultValidationError(
                f"{ph}: finding {fid} classification must be one of "
                f"{FINDING_CLASSIFICATIONS}, got {cls_!r}"
            )
        return cls(
            id=fid,
            classification=cls_,
            required_resolution=_multi_line(
                _bounded(
                    _req_str(raw, "required_resolution", ph),
                    ph,
                    subject,
                    "required_resolution",
                    MAX_FINDING_RESOLUTION_CHARS,
                ),
                ph,
                subject,
                "required_resolution",
            ),
            title=_one_line(
                _bounded(_opt_str(raw, "title", ph), ph, subject, "title", MAX_FINDING_TITLE_CHARS),
                ph,
                subject,
                "title",
            ),
            location=_one_line(
                _bounded(
                    _opt_str(raw, "location", ph),
                    ph,
                    subject,
                    "location",
                    MAX_FINDING_LOCATION_CHARS,
                ),
                ph,
                subject,
                "location",
            ),
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "classification": self.classification,
            "required_resolution": self.required_resolution,
            "title": self.title,
            "location": self.location,
        }


@dataclass
class ReviewResult:
    """What a REMOTE reviewer reports; the controller publishes it (#162).

    ``round`` and ``reviewed_head_sha`` are cross-checks against the round
    and the HEAD the controller bound; neither is a target, and the result
    names no comment. ``needs_fix_round`` and ``findings`` are the verdict.
    ``sections`` holds the prose of :data:`REVIEW_PROSE_SECTIONS`, in that
    order. Every field the controller renders into the review comment (the
    prose, and each finding's ``title``, ``location`` and
    ``required_resolution``) is held to the published-content policy; the
    findings' shape and bounds are LOCAL's too, unchanged.
    """

    round: int
    reviewed_head_sha: str
    needs_fix_round: bool
    findings: list[Finding] = field(default_factory=list)
    sections: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_payload(cls, p: dict) -> ReviewResult:
        ph = "REVIEW"
        rnd = _req(p, "round", ph)
        if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
            raise ControlResultValidationError("'round' must be an int >= 1")
        needs_fix = _req_bool(p, "needs_fix_round", ph)
        findings = _parse_findings(p, rnd, needs_fix)
        for finding in findings:
            check_published_finding(finding)
        sections = {
            key: validate_review_section(key, p.get(key)) for key, _ in REVIEW_PROSE_SECTIONS
        }
        return cls(
            round=rnd,
            reviewed_head_sha=_req_sha(p, "reviewed_head_sha", ph),
            needs_fix_round=needs_fix,
            findings=findings,
            sections=sections,
        )


def check_published_finding(finding: Finding) -> None:
    """Refuse ``finding`` unless the round's review comment may show it (#162).

    Each field is judged as the comment renders it: the location inside a
    code span, the others as Markdown text. A persisted REVIEW plan's
    findings are held to the same rule when they are loaded (ADR 0004 D4.6).
    """
    _review_published(finding.title, f"{finding.id}.title")
    _review_published(markdown_code_span(finding.location), f"{finding.id}.location")
    if credential_classes(finding.location):
        # The span's backticks must not hide a shape the bare value has: the
        # findings are persisted as reported, in plain state.
        _review_published(finding.location, f"{finding.id}.location")
    _review_published(finding.required_resolution, f"{finding.id}.required_resolution")


def validate_review_section(key: str, text: object) -> str:
    """``text`` as REVIEW prose section ``key``, or a rejection (#162).

    Non-blank, within the section's bound (:data:`REVIEW_PROSE_SECTIONS`),
    multi-line text with no other control character, and publishable
    (:func:`published_text_problem`): the controller renders it into the
    round's review comment under its fixed heading. The checks
    :meth:`ReviewResult.from_payload` applies, with the same messages, so a
    persisted section can be re-validated under the parser's rules.
    """
    ph = "REVIEW"
    limit = dict(REVIEW_PROSE_SECTIONS)[key]
    stripped = _req_str({key: text}, key, ph)
    stripped = _multi_line(_bounded(stripped, ph, "result", key, limit), ph, "result", key)
    return _review_published(stripped, key)


def _review_published(text: str, subject: str) -> str:
    """Refuse REVIEW text ``subject`` under the published-content policy."""
    problem = published_text_problem(subject, text)
    if problem is not None:
        raise ControlResultValidationError(f"REVIEW: field {problem}")
    return text


def _fix_text(text: object, subject: str, key: str, limit: int, one_line: bool) -> str:
    """A FIX follow-up text the controller publishes: non-blank, bounded, publishable."""
    ph = "FIX"
    if not isinstance(text, str):
        raise ControlResultValidationError(f"{ph}: {subject} field {key!r} must be a string")
    stripped = text.strip()
    if not stripped:
        raise ControlResultValidationError(f"{ph}: {subject} is missing required field {key!r}")
    stripped = _bounded(stripped, ph, subject, key, limit)
    stripped = (_one_line if one_line else _multi_line)(stripped, ph, subject, key)
    problem = published_text_problem(f"{subject} {key}", stripped)
    if problem is not None:
        raise ControlResultValidationError(f"{ph}: field {problem}")
    return stripped


def validate_follow_up_title(text: object, subject: str = "follow_up_issue") -> str:
    """``text`` as the title of a new follow-up issue (#163), or a rejection.

    Non-blank, at most :data:`MAX_FOLLOW_UP_TITLE_CHARS`, one line of
    printable text, and publishable (:func:`published_text_problem`): the
    controller creates the issue with it. The checks
    :meth:`FindingResolution.from_payload` applies, so a persisted K5 title
    can be re-validated under the parser's rules.
    """
    return _fix_text(text, subject, "title", MAX_FOLLOW_UP_TITLE_CHARS, one_line=True)


def validate_follow_up_body(text: object, subject: str = "follow_up_issue") -> str:
    """``text`` as the agent's part of a new follow-up issue's body (#163), or a rejection.

    Non-blank, at most :data:`MAX_FOLLOW_UP_BODY_CHARS`, multi-line text
    with no other control character, and publishable: the controller
    follows it with its own reference to the PR and finding and the
    finding's marker. The checks :meth:`FindingResolution.from_payload`
    applies, so the agent's part of a persisted K5 body can be re-validated.
    """
    return _fix_text(text, subject, "body", MAX_FOLLOW_UP_BODY_CHARS, one_line=False)


@dataclass
class FindingResolution:
    """One open finding's resolution, as a REMOTE fixer reports it (#163).

    A ``follow_up_created`` resolution either names an issue the controller
    handed over (``follow_up_issue_url``) or carries the title and body of a
    new follow-up issue the controller creates (``follow_up_title``,
    ``follow_up_body``; the payload's ``follow_up_issue`` object), never
    both. ``commit_sha`` is allowed on ``fixed`` only. ``to_dict`` keeps the
    persisted shape of ``state.last_fix_resolutions``.
    """

    finding_id: str
    resolution: str
    rationale: str = ""
    follow_up_issue_url: str = ""
    commit_sha: str = ""
    follow_up_title: str = ""
    follow_up_body: str = ""

    @property
    def new_follow_up(self) -> bool:
        """A follow-up issue the controller creates for this finding."""
        return bool(self.follow_up_title)

    @classmethod
    def from_payload(cls, raw: object, index: int) -> FindingResolution:
        ph = "FIX"
        if not isinstance(raw, dict):
            raise ControlResultValidationError(
                f"{ph}: resolutions[{index}] must be an object with finding_id/resolution"
            )
        fid = _finding_id(raw, "finding_id", ph)
        res = _req_str(raw, "resolution", ph)
        if res not in FIX_RESOLUTIONS:
            raise ControlResultValidationError(
                f"{ph}: resolution for {fid} must be one of {FIX_RESOLUTIONS}, got {res!r}"
            )
        subject = f"resolution for {fid}"
        rationale = _multi_line(
            _bounded(
                _opt_str(raw, "rationale", ph), ph, subject, "rationale", MAX_FIX_RATIONALE_CHARS
            ),
            ph,
            subject,
            "rationale",
        )
        follow_up = _opt_url(raw, "follow_up_issue_url", ph, "issue")
        new_issue = raw.get("follow_up_issue")
        commit_sha = _opt_sha(raw, "commit_sha", ph)
        if res == "no_change_with_rationale":
            if len(rationale) < MIN_RATIONALE_CHARS:
                raise ControlResultValidationError(
                    f"{ph}: {fid} uses no_change_with_rationale but the rationale is missing "
                    f"or too short (>= {MIN_RATIONALE_CHARS} chars of actual reasoning required)"
                )
        if res != "follow_up_created":
            if follow_up or new_issue is not None:
                raise ControlResultValidationError(
                    f"{ph}: {fid} carries a follow-up issue but resolution is {res!r}"
                )
        elif bool(follow_up) == (new_issue is not None):
            raise ControlResultValidationError(
                f"{ph}: {fid} uses follow_up_created and must carry exactly one of "
                "'follow_up_issue_url' (an issue the prompt lists) and 'follow_up_issue' "
                "(the title and body of a new issue the controller creates)"
            )
        if commit_sha and res != "fixed":
            raise ControlResultValidationError(
                f"{ph}: {fid} carries commit_sha but resolution is {res!r}; only a fixed "
                "finding names the commit that fixed it"
            )
        title = body = ""
        if new_issue is not None:
            if not isinstance(new_issue, dict):
                raise ControlResultValidationError(
                    f"{ph}: {fid} field 'follow_up_issue' must be an object with title and body"
                )
            what = f"follow_up_issue of {fid}"
            title = validate_follow_up_title(new_issue.get("title"), what)
            body = validate_follow_up_body(new_issue.get("body"), what)
        return cls(
            finding_id=fid,
            resolution=res,
            rationale=rationale,
            follow_up_issue_url=follow_up,
            commit_sha=commit_sha,
            follow_up_title=title,
            follow_up_body=body,
        )

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "resolution": self.resolution,
            "rationale": self.rationale,
            "follow_up_issue_url": self.follow_up_issue_url,
            "commit_sha": self.commit_sha,
        }


@dataclass
class FixResult:
    """What a REMOTE fixer reports; the controller publishes it (#163).

    ``previous_head_sha`` and ``head_sha`` are cross-checks against the
    reviewed HEAD and the worktree's ``HEAD``, which the controller reads
    itself; neither is a target. ``resolutions`` holds one resolution per
    open finding, ``tests`` names what the agent ran.
    """

    previous_head_sha: str
    head_sha: str
    resolutions: list[FindingResolution] = field(default_factory=list)
    tests: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, p: dict) -> FixResult:
        ph = "FIX"
        previous = _req_sha(p, "previous_head_sha", ph)
        head = _req_sha(p, "head_sha", ph)
        resolutions = [
            FindingResolution.from_payload(r, i) for i, r in enumerate(_raw_resolutions(p))
        ]
        ids = [r.finding_id for r in resolutions]
        if len(set(ids)) != len(ids):
            raise ControlResultValidationError(f"duplicate resolution finding_ids: {ids}")
        tests = _opt_str_list(p, "tests", ph)
        if len(tests) > MAX_TESTS_REPORTED:
            raise ControlResultValidationError(
                f"{ph}: field 'tests' lists {len(tests)} entries; the controller accepts at "
                f"most {MAX_TESTS_REPORTED}. Summarise them and re-emit the CONTROL_RESULT."
            )
        for test in tests:
            _one_line(_bounded(test, ph, "result", "tests", MAX_TEST_CHARS), ph, "result", "tests")
        return cls(previous_head_sha=previous, head_sha=head, resolutions=resolutions, tests=tests)


@dataclass
class ReplanReexecuteResult:
    """What a replan agent reports; the controller publishes it (#164).

    ``issue_url``, ``previous_pr_url``, ``previous_branch``,
    ``previous_head_sha`` and ``execution_attempt`` are cross-checks against
    the transaction's checkpoint, and ``head_sha`` against the worktree's
    ``HEAD``, which the controller reads itself; none of them is a target.
    The counts and ``verification.tests_passed`` become the transaction
    marker the controller renders. ``pr_title`` and ``pr_body`` are the
    agent's text for the replacement PR the controller creates, under the
    same bounds and content policy as ANALYZE_EXECUTE's: the controller
    appends ``Closes #n``, the implementation marker and the transaction
    marker itself. The result names no replacement PR, branch or push target.
    """

    issue_url: str
    previous_pr_url: str
    previous_branch: str
    previous_head_sha: str
    head_sha: str
    execution_attempt: int
    historical_findings_considered: int
    unique_failure_constraints: int
    fresh_review_round: int
    tests_run: list[str]
    tests_passed: bool
    pr_title: str
    pr_body: str

    @classmethod
    def from_payload(cls, p: dict) -> ReplanReexecuteResult:
        ph = "REPLAN_REEXECUTE"
        previous_pr = _req_url(p, "previous_pr_url", ph, "pr")
        previous_branch = _req_str(p, "previous_branch", ph)
        ints: dict[str, int] = {}
        for key in (
            "execution_attempt",
            "historical_findings_considered",
            "unique_failure_constraints",
            "fresh_review_round",
        ):
            value = _req(p, key, ph)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ControlResultValidationError(f"{ph}: field {key!r} must be an integer >= 0")
            ints[key] = value
        if ints["execution_attempt"] < 1:
            raise ControlResultValidationError("REPLAN_REEXECUTE: execution_attempt must be >= 1")
        if ints["fresh_review_round"] != 1:
            raise ControlResultValidationError("REPLAN_REEXECUTE: fresh_review_round must equal 1")
        if ints["unique_failure_constraints"] > ints["historical_findings_considered"]:
            raise ControlResultValidationError(
                "REPLAN_REEXECUTE: unique_failure_constraints cannot exceed "
                "historical_findings_considered"
            )
        if _req_str(p, "previous_pr_disposition", ph) != "superseded":
            raise ControlResultValidationError(
                "REPLAN_REEXECUTE: previous_pr_disposition must be 'superseded'"
            )
        verification = _req(p, "verification", ph)
        if not isinstance(verification, dict):
            raise ControlResultValidationError("REPLAN_REEXECUTE: verification must be an object")
        tests_run = verification.get("tests_run")
        tests_passed = verification.get("tests_passed")
        if not isinstance(tests_run, list) or not all(isinstance(test, str) for test in tests_run):
            raise ControlResultValidationError(
                "REPLAN_REEXECUTE: verification.tests_run must be a list"
            )
        if not isinstance(tests_passed, bool):
            raise ControlResultValidationError(
                "REPLAN_REEXECUTE: verification.tests_passed must be a boolean"
            )
        return cls(
            issue_url=_req_url(p, "issue_url", ph, "issue"),
            previous_pr_url=previous_pr,
            previous_branch=previous_branch,
            previous_head_sha=_req_sha(p, "previous_head_sha", ph),
            head_sha=_req_sha(p, "head_sha", ph),
            execution_attempt=ints["execution_attempt"],
            historical_findings_considered=ints["historical_findings_considered"],
            unique_failure_constraints=ints["unique_failure_constraints"],
            fresh_review_round=1,
            tests_run=tests_run,
            tests_passed=tests_passed,
            pr_title=validate_pr_title(_req_str(p, "pr_title", ph), phase=ph),
            pr_body=validate_pr_body(_req_str(p, "pr_body", ph), phase=ph),
        )


# Phases that never produce a CONTROL_RESULT: INITIALIZING and
# READY_FOR_MERGE are deterministic, and MERGE is executed by the controller
# itself (gh pr merge), never by an agent.
NON_AGENT_PHASES = frozenset(
    {
        Phase.INITIALIZING,
        Phase.READY_FOR_MERGE,
        Phase.MERGE,
        Phase.DONE,
        Phase.BLOCKED,
        Phase.FAILED,
    }
)


# The keys every re-request accepts besides the fields it asks for (D4.7).
_RE_REQUEST_ENVELOPE = ("phase", "status", "message")
_RE_REQUEST_FIELDS: dict[UpdateEpicRequest, tuple[str, ...]] = {
    UpdateEpicRequest.SELECTION: ("next_issue_url",),
    UpdateEpicRequest.SELECTION_WITH_ROADMAP: ("next_issue_url", "roadmap_section"),
    UpdateEpicRequest.ROADMAP: ("roadmap_section",),
}
# The UPDATE_EPIC field names a refusal may quote: they are the controller's
# own. Any other key is agent text and is only counted.
_UPDATE_EPIC_FIELDS = ("progress", "roadmap_section", "next_issue_url")


def validate_progress_text(text: str) -> str:
    """``text`` as an UPDATE_EPIC ``progress`` field, or a rejection.

    Non-blank, at most :data:`MAX_PROGRESS_CHARS`, multi-line text with no
    other control character, publishable (:func:`published_text_problem`),
    and with no URL (:data:`_PROGRESS_URL_RE`): the controller posts it as
    the EPIC progress comment. The checks
    :meth:`UpdateEpicResult.from_payload` applies, with the same messages,
    so a persisted value can be re-validated under the parser's rules.
    """
    if not isinstance(text, str):
        raise ControlResultValidationError("UPDATE_EPIC: field 'progress' must be a string")
    if not text.strip():
        raise ControlResultValidationError(
            "CONTROL_RESULT for UPDATE_EPIC missing required field 'progress'"
        )
    text = _bounded(text, "UPDATE_EPIC", "result", "progress", MAX_PROGRESS_CHARS)
    text = _multi_line(text, "UPDATE_EPIC", "result", "progress")
    text = _published_field(text, "progress", "UPDATE_EPIC")
    m = _PROGRESS_URL_RE.search(text)
    if m is not None:
        raise ControlResultValidationError(
            f"UPDATE_EPIC: field 'progress' contains a URL at index {m.start()} ('://' or "
            "'www.'), even inside code; the progress comment carries no URL, and the "
            "controller's marker names the issue and PR. Refer to them as '#n' instead, and "
            "re-emit the CONTROL_RESULT."
        )
    return text


def validate_roadmap_section(text: str) -> str:
    """``text`` as a present UPDATE_EPIC ``roadmap_section``, or a rejection.

    Non-blank, at most :data:`MAX_ROADMAP_SECTION_CHARS`, multi-line text,
    no ``<!-- ai-`` marker (the controller writes the roadmap markers around
    the section itself), and publishable (:func:`published_text_problem`).
    The checks :meth:`UpdateEpicResult.from_payload` applies, with the same
    messages.
    """
    if not isinstance(text, str):
        raise ControlResultValidationError(
            "UPDATE_EPIC: optional field 'roadmap_section' must be a string when present, "
            f"got {type(text).__name__}"
        )
    if not text.strip():
        raise ControlResultValidationError(
            "CONTROL_RESULT for UPDATE_EPIC missing required field 'roadmap_section'"
        )
    text = _bounded(text, "UPDATE_EPIC", "result", "roadmap_section", MAX_ROADMAP_SECTION_CHARS)
    text = _multi_line(text, "UPDATE_EPIC", "result", "roadmap_section")
    if CONTROLLER_MARKER_OPEN_RE.search(text):
        raise ControlResultValidationError(
            "UPDATE_EPIC: field 'roadmap_section' must not contain a controller "
            f"marker ({_CONTROLLER_MARKER_PREFIX}...): the controller writes "
            f"{ROADMAP_START_MARKER!r} and {ROADMAP_END_MARKER!r} around the section "
            "itself. Return only the section's content and re-emit the CONTROL_RESULT."
        )
    return _published_field(text, "roadmap_section", "UPDATE_EPIC")


def validate_next_issue_url(url: str) -> str:
    """``url`` as a present UPDATE_EPIC ``next_issue_url``, or a rejection.

    Shape and length at parse time (the ``next_issue_url`` half of #15).
    Which issue the URL names is the engine's to verify (repository, EPIC,
    finished issue, exists, OPEN); whether the string is an issue URL at all
    is a malformed result, corrected like any other rather than spent as a
    selection, and an oversized value is never quoted into
    ``next_issue_rejections``, the re-selection prompt or the run log.
    """
    if not isinstance(url, str):
        raise ControlResultValidationError("'next_issue_url' must be a string or null")
    return _checked_url(url, "next_issue_url", "UPDATE_EPIC", "issue")


def _refuse_outside_re_request(p: dict, request: UpdateEpicRequest) -> None:
    """Refuse a re-request result carrying a key outside its schema (D4.7)."""
    asked = _RE_REQUEST_FIELDS[request]
    allowed = (*_RE_REQUEST_ENVELOPE, *asked)
    extra = [key for key in p if key not in allowed]
    if not extra:
        return
    named = [key for key in _UPDATE_EPIC_FIELDS if key in extra]
    others = len(extra) - len(named)
    carried = [repr(key) for key in named]
    if others:
        carried.append(f"{others} other key{'s' if others > 1 else ''}")
    accepted = ", ".join(repr(key) for key in allowed)
    asked_for = " and ".join(repr(key) for key in asked)
    raise ControlResultValidationError(
        f"UPDATE_EPIC: this re-request accepts only {accepted}, and the result also "
        f"carries {', '.join(carried)}. The published progress comment is already done and "
        f"this re-request asks only for {asked_for}. Remove the other keys and re-emit the "
        "CONTROL_RESULT."
    )


def _next_issue_url(p: dict) -> str | None:
    """The ``next_issue_url`` key, required; ``null`` or ``""`` is ``None``."""
    if "next_issue_url" not in p:
        raise ControlResultValidationError(
            "CONTROL_RESULT for UPDATE_EPIC missing required field "
            "'next_issue_url' (use null when the epic is complete)"
        )
    nxt = p["next_issue_url"]
    if nxt is None or nxt == "":
        return None
    return validate_next_issue_url(nxt)


@dataclass
class UpdateEpicResult:
    next_issue_url: str | None
    # The new content of the EPIC's managed roadmap section, or ``None`` when
    # the agent returned none (absent, ``null`` or blank). Whether one is
    # required is the engine's decision (``workflow.epic_update_every``);
    # the parser only bounds and shapes it. It is the *content between* the
    # markers: the markers themselves are written by the controller.
    roadmap_section: str | None = None
    # The EPIC progress comment's text: required in a ``FULL`` result,
    # ``None`` in a re-request, which never carries progress (D4.7).
    progress: str | None = None

    @classmethod
    def from_payload(
        cls, p: dict, request: UpdateEpicRequest = UpdateEpicRequest.FULL
    ) -> UpdateEpicResult:
        """Validate ``p`` against the schema of ``request``.

        ``FULL`` ignores unknown keys, as every phase schema does; a
        re-request refuses any key outside its own schema, a ``null`` one
        included.
        """
        request = UpdateEpicRequest(request)
        if request != UpdateEpicRequest.FULL:
            _refuse_outside_re_request(p, request)
        section: str | None = _opt_str(p, "roadmap_section", "UPDATE_EPIC") or None
        if section is not None:
            section = validate_roadmap_section(section)
        elif request == UpdateEpicRequest.ROADMAP:
            raise ControlResultValidationError(
                "CONTROL_RESULT for UPDATE_EPIC missing required field 'roadmap_section' "
                "(this re-request asks only for the roadmap section)"
            )
        nxt = None if request == UpdateEpicRequest.ROADMAP else _next_issue_url(p)
        progress = None
        if request == UpdateEpicRequest.FULL:
            progress = validate_progress_text(_req_str(p, "progress", "UPDATE_EPIC"))
        return cls(next_issue_url=nxt, roadmap_section=section, progress=progress)


# -- LOCAL-mode typed models ----------------------------------------------
# A local phase has no PR, no comment and no follow-up Issue, so it gets its
# own small shapes instead of remote fields stuffed with empty strings or
# fabricated URLs. Everything the controller can observe itself (HEAD, the
# changed-file list, the fingerprint) is observed, not reported; the agent
# reports only what it alone knows, plus the one or two claims worth
# cross-checking against the controller's own observation.


def _opt_str_list(payload: dict, key: str, phase: str) -> list[str]:
    raw = payload.get(key, [])
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        raise ControlResultValidationError(f"{phase}: field {key!r} must be a list of strings")
    return [x.strip() for x in raw if x.strip()]


def _req_fingerprint(payload: dict, key: str, phase: str) -> str:
    v = _req_str(payload, key, phase)
    if not _FINGERPRINT_RE.match(v.lower()):
        raise ControlResultValidationError(
            f"{phase}: field {key!r} must be the 64-character workspace fingerprint the "
            f"controller provided, got {v!r}"
        )
    return v.lower()


@dataclass
class LocalAnalyzeExecuteResult:
    """What an implementation agent alone knows about a local run."""

    summary: str
    changed_workspace: bool
    tests_attempted: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, p: dict) -> LocalAnalyzeExecuteResult:
        ph = "ANALYZE_EXECUTE"
        return cls(
            summary=_req_str(p, "summary", ph),
            # Cross-checked against the controller's own before/after
            # fingerprint: a claim of "I changed nothing" over a modified
            # tree (or the reverse) is a rejected result, not a nuance.
            changed_workspace=_req_bool(p, "changed_workspace", ph),
            tests_attempted=_opt_str_list(p, "tests_attempted", ph),
        )


@dataclass
class LocalReviewResult:
    round: int
    reviewed_workspace_fingerprint: str
    needs_fix_round: bool
    findings: list[Finding] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    @classmethod
    def from_payload(cls, p: dict) -> LocalReviewResult:
        ph = "REVIEW"
        rnd = _req(p, "round", ph)
        if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
            raise ControlResultValidationError("'round' must be an int >= 1")
        needs_fix = _req_bool(p, "needs_fix_round", ph)
        findings = _parse_findings(p, rnd, needs_fix)
        return cls(
            round=rnd,
            # Binds the review to exactly the workspace the controller
            # fingerprinted before invoking the reviewer — the local
            # analogue of binding a remote review to the PR HEAD SHA.
            reviewed_workspace_fingerprint=_req_fingerprint(
                p, "reviewed_workspace_fingerprint", ph
            ),
            needs_fix_round=needs_fix,
            findings=findings,
            observations=_opt_str_list(p, "observations", ph),
        )


@dataclass
class LocalFindingResolution:
    finding_id: str
    resolution: str
    rationale: str = ""

    @classmethod
    def from_payload(cls, raw: object, index: int) -> LocalFindingResolution:
        ph = "FIX"
        if not isinstance(raw, dict):
            raise ControlResultValidationError(
                f"{ph}: resolutions[{index}] must be an object with finding_id/resolution"
            )
        fid = _finding_id(raw, "finding_id", ph)
        res = _req_str(raw, "resolution", ph)
        if res not in LOCAL_FIX_RESOLUTIONS:
            raise ControlResultValidationError(
                f"{ph}: resolution for {fid} must be one of {LOCAL_FIX_RESOLUTIONS}, got {res!r}"
                + (
                    " — local runs have no GitHub, so a finding cannot be deferred to a "
                    "follow-up Issue; use 'unresolved' with a rationale instead"
                    if res == "follow_up_created"
                    else ""
                )
            )
        subject = f"resolution for {fid}"
        rationale = _multi_line(
            _bounded(
                _opt_str(raw, "rationale", ph), ph, subject, "rationale", MAX_FIX_RATIONALE_CHARS
            ),
            ph,
            subject,
            "rationale",
        )
        # Both non-fix dispositions are only acceptable with real reasoning:
        # "won't fix" and "couldn't fix" are decisions a human has to judge.
        if res in ("no_change_with_rationale", "unresolved") and len(rationale) < (
            MIN_RATIONALE_CHARS
        ):
            raise ControlResultValidationError(
                f"{ph}: {fid} uses {res} but the rationale is missing or too short "
                f"(>= {MIN_RATIONALE_CHARS} chars of actual reasoning required)"
            )
        if "follow_up_issue_url" in raw:
            raise ControlResultValidationError(
                f"{ph}: {fid} carries 'follow_up_issue_url'; local runs never create "
                "GitHub follow-up issues"
            )
        return cls(finding_id=fid, resolution=res, rationale=rationale)

    def to_dict(self) -> dict:
        return {
            "finding_id": self.finding_id,
            "resolution": self.resolution,
            "rationale": self.rationale,
        }

    @property
    def is_resolved(self) -> bool:
        return self.resolution in ("fixed", "no_change_with_rationale")


@dataclass
class LocalFixResult:
    resolutions: list[LocalFindingResolution] = field(default_factory=list)
    changed_workspace: bool = False

    @classmethod
    def from_payload(cls, p: dict) -> LocalFixResult:
        ph = "FIX"
        resolutions = [
            LocalFindingResolution.from_payload(r, i) for i, r in enumerate(_raw_resolutions(p))
        ]
        ids = [r.finding_id for r in resolutions]
        if len(set(ids)) != len(ids):
            raise ControlResultValidationError(f"duplicate resolution finding_ids: {ids}")
        # `from_payload` only runs for status "success" (see
        # `parse_control_result`), so a non-empty run-level blocker here is a
        # result that contradicts itself: the protocol already carries one,
        # as status "blocked" plus a message, and the engine routes that to
        # BLOCKED. Accepting it alongside "success" gave the field nowhere to
        # go -- it was parsed and dropped, so a FIX could report a blocker,
        # pass validation, be reviewed clean and reach DONE with the blocker
        # never seen by anyone. Rejecting it puts the claim back on the one
        # channel the controller acts on rather than quietly keeping both.
        blocker = p.get("blocked_reason")
        if isinstance(blocker, str) and blocker.strip():
            raise ControlResultValidationError(
                f"{ph}: status 'success' carries a non-empty 'blocked_reason' "
                f"({blocker.strip()[:200]!r}); a run-level blocker and a successful fix are "
                "not both true. Report the obstacle as status 'blocked' with a 'message', or "
                "report the finding it concerns with resolution 'unresolved' and a rationale"
            )
        if blocker is not None and not isinstance(blocker, str):
            raise ControlResultValidationError(f"{ph}: field 'blocked_reason' must be a string")
        return cls(
            resolutions=resolutions,
            changed_workspace=_req_bool(p, "changed_workspace", ph),
        )


# Phases a LOCAL run can never reach; they belong to the GitHub lifecycle.
LOCAL_UNSUPPORTED_PHASES = frozenset(
    {Phase.REPLAN_REEXECUTE, Phase.READY_FOR_MERGE, Phase.MERGE, Phase.UPDATE_EPIC}
)


def _validate_local_phase(phase: Phase, payload: dict) -> None:
    if phase == Phase.ANALYZE_EXECUTE:
        LocalAnalyzeExecuteResult.from_payload(payload)
    elif phase == Phase.REVIEW:
        LocalReviewResult.from_payload(payload)
    elif phase == Phase.FIX:
        LocalFixResult.from_payload(payload)
    elif phase in LOCAL_UNSUPPORTED_PHASES:
        raise ControlResultValidationError(
            f"phase {phase.value} belongs to the REMOTE (GitHub) workflow and is never "
            "executed by a local run"
        )
    else:
        raise ControlResultValidationError(
            f"phase {phase.value} is executed by the controller and never accepts an "
            "agent CONTROL_RESULT"
        )


def validate_for_phase(
    phase: Phase,
    payload: dict,
    mode: WorkflowMode = WorkflowMode.REMOTE,
    *,
    update_epic_request: UpdateEpicRequest = UpdateEpicRequest.FULL,
) -> None:
    """Enforce the per-phase required-fields schema (raises on violation).

    ``update_epic_request`` selects the UPDATE_EPIC schema; other phases
    ignore it.
    """
    if mode == WorkflowMode.LOCAL:
        _validate_local_phase(phase, payload)
        return
    if phase == Phase.ANALYZE_EXECUTE:
        AnalyzeExecuteResult.from_payload(payload)
    elif phase == Phase.REVIEW:
        ReviewResult.from_payload(payload)
    elif phase == Phase.FIX:
        FixResult.from_payload(payload)
    elif phase == Phase.REPLAN_REEXECUTE:
        ReplanReexecuteResult.from_payload(payload)
    elif phase == Phase.UPDATE_EPIC:
        UpdateEpicResult.from_payload(payload, update_epic_request)
    elif phase in NON_AGENT_PHASES:
        raise ControlResultValidationError(
            f"phase {phase.value} is executed by the controller and never accepts an "
            "agent CONTROL_RESULT"
        )
