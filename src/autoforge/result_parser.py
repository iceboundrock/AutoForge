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
:func:`published_payload_problem` a whole rendered payload and
:func:`commit_message_problem` a commit message; each names the subject and
the rule, never the text. An UPDATE_EPIC result is validated against the
schema of the request that launched the agent (:class:`UpdateEpicRequest`,
D4.7): a re-request after publication carries only what it asks for.
"""

from __future__ import annotations

import json
import re
from collections import deque
from collections.abc import Iterable, Iterator
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


def _mention_problem(subject: str, text: str) -> str | None:
    """The refusal of the first ``@``-mention outside code in ``text``, if any."""
    mentions = [m.start() for m in _MENTION_RE.finditer(text)]
    if not mentions:
        return None
    ranges = _code_ranges(text)
    outside = next(_outside(ranges, mentions), None)
    raw_html = next(_outside(ranges, (m.start() for m in _RAW_HTML_RE.finditer(text))), None)
    if raw_html is not None and outside != mentions[0]:
        return (
            f"{subject!r} contains an @-mention at index {mentions[0]} and raw HTML (a tag, "
            f"comment or autolink) outside code at index {raw_html}; GitHub may read code "
            "near raw HTML as text, so the code exemption does not apply. Remove the HTML or "
            "the mention and re-emit the CONTROL_RESULT."
        )
    if outside is None:
        return None
    return (
        f"{subject!r} contains an @-mention at index {outside} outside a code span or fenced "
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
    issue_url: str
    pr_url: str
    head_sha: str
    branch: str

    @classmethod
    def from_payload(cls, p: dict) -> AnalyzeExecuteResult:
        ph = "ANALYZE_EXECUTE"
        return cls(
            issue_url=_req_url(p, "issue_url", ph, "issue"),
            pr_url=_req_url(p, "pr_url", ph, "pr"),
            head_sha=_req_sha(p, "head_sha", ph),
            branch=_req_str(p, "branch", ph),
        )


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
    round: int
    reviewed_head_sha: str
    review_comment_url: str
    needs_fix_round: bool
    findings: list[Finding] = field(default_factory=list)

    @classmethod
    def from_payload(cls, p: dict) -> ReviewResult:
        ph = "REVIEW"
        rnd = _req(p, "round", ph)
        if not isinstance(rnd, int) or isinstance(rnd, bool) or rnd < 1:
            raise ControlResultValidationError("'round' must be an int >= 1")
        needs_fix = _req_bool(p, "needs_fix_round", ph)
        findings = _parse_findings(p, rnd, needs_fix)
        return cls(
            round=rnd,
            reviewed_head_sha=_req_sha(p, "reviewed_head_sha", ph),
            review_comment_url=_req_url(p, "review_comment_url", ph, "comment"),
            needs_fix_round=needs_fix,
            findings=findings,
        )


@dataclass
class FindingResolution:
    finding_id: str
    resolution: str
    rationale: str = ""
    follow_up_issue_url: str = ""
    commit_sha: str = ""

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
        if res == "no_change_with_rationale":
            if len(rationale) < MIN_RATIONALE_CHARS:
                raise ControlResultValidationError(
                    f"{ph}: {fid} uses no_change_with_rationale but the rationale is missing "
                    f"or too short (>= {MIN_RATIONALE_CHARS} chars of actual reasoning required)"
                )
        if res == "follow_up_created" and not follow_up:
            raise ControlResultValidationError(
                f"{ph}: {fid} uses follow_up_created but 'follow_up_issue_url' is missing"
            )
        if res != "follow_up_created" and follow_up:
            raise ControlResultValidationError(
                f"{ph}: {fid} carries follow_up_issue_url but resolution is {res!r}"
            )
        return cls(
            finding_id=fid,
            resolution=res,
            rationale=rationale,
            follow_up_issue_url=follow_up,
            commit_sha=_opt_sha(raw, "commit_sha", ph),
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
    previous_head_sha: str
    new_head_sha: str
    resolutions: list[FindingResolution] = field(default_factory=list)

    @classmethod
    def from_payload(cls, p: dict) -> FixResult:
        ph = "FIX"
        resolutions = [
            FindingResolution.from_payload(r, i) for i, r in enumerate(_raw_resolutions(p))
        ]
        ids = [r.finding_id for r in resolutions]
        if len(set(ids)) != len(ids):
            raise ControlResultValidationError(f"duplicate resolution finding_ids: {ids}")
        return cls(
            previous_head_sha=_req_sha(p, "previous_head_sha", ph),
            new_head_sha=_req_sha(p, "new_head_sha", ph),
            resolutions=resolutions,
        )


@dataclass
class ReplanReexecuteResult:
    issue_url: str
    previous_pr_url: str
    replacement_pr_url: str
    previous_branch: str
    replacement_branch: str
    previous_head_sha: str
    replacement_head_sha: str
    execution_attempt: int
    historical_findings_considered: int
    unique_failure_constraints: int
    fresh_review_round: int
    tests_run: list[str]
    tests_passed: bool

    @classmethod
    def from_payload(cls, p: dict) -> ReplanReexecuteResult:
        ph = "REPLAN_REEXECUTE"
        previous_pr = _req_url(p, "previous_pr_url", ph, "pr")
        replacement_pr = _req_url(p, "replacement_pr_url", ph, "pr")
        if parse_pr_url(previous_pr).same_target(parse_pr_url(replacement_pr)):
            raise ControlResultValidationError(
                "REPLAN_REEXECUTE: replacement_pr_url must differ from previous_pr_url"
            )
        previous_branch = _req_str(p, "previous_branch", ph)
        replacement_branch = _req_str(p, "replacement_branch", ph)
        if previous_branch == replacement_branch:
            raise ControlResultValidationError(
                "REPLAN_REEXECUTE: replacement_branch must differ from previous_branch"
            )
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
            replacement_pr_url=replacement_pr,
            previous_branch=previous_branch,
            replacement_branch=replacement_branch,
            previous_head_sha=_req_sha(p, "previous_head_sha", ph),
            replacement_head_sha=_req_sha(p, "replacement_head_sha", ph),
            execution_attempt=ints["execution_attempt"],
            historical_findings_considered=ints["historical_findings_considered"],
            unique_failure_constraints=ints["unique_failure_constraints"],
            fresh_review_round=1,
            tests_run=tests_run,
            tests_passed=tests_passed,
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


def _published_field(text: str, key: str) -> str:
    """Refuse UPDATE_EPIC field ``key`` under the published-content policy."""
    problem = published_text_problem(key, text)
    if problem is not None:
        raise ControlResultValidationError(f"UPDATE_EPIC: field {problem}")
    return text


def validate_progress_text(text: str) -> str:
    """``text`` as an UPDATE_EPIC ``progress`` field, or a rejection.

    Non-blank, at most :data:`MAX_PROGRESS_CHARS`, multi-line text with no
    other control character, and publishable (:func:`published_text_problem`):
    the controller posts it as the EPIC progress comment. The checks
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
    return _published_field(text, "progress")


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
    return _published_field(text, "roadmap_section")


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
