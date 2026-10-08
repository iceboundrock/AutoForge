"""Controller-owned external effects: records, completion contexts, decisions.

ADR 0004 moves every externally visible write of a phase from the agent to
the controller. This module is the pure part of that layer (D12.1): the
persisted shapes, their validation on load, and the reconciliation
decisions as functions of (record, what was read back). It does no I/O,
knows no provider and speaks no ``gh``; the reads and writes live in
:mod:`autoforge.effect_ops`, :mod:`autoforge.github` and
:mod:`autoforge.git_transport`.

The vocabulary
--------------
An **effect** is one controller write to one exact remote object (D2.1). Its
**record** (:class:`EffectRecord`) holds the kind, a deterministic identity,
the exact target, the precondition, the payload (the exact bytes to send,
redaction-invariant), the completion criterion, the attempt count, the
stage, the observed result, the owner binding and the record's position in
the phase's ordered plan (D2.2). The stages are closed (D2.3)::

    intended  -> attempted -> observed
         \\            \\----> conflict
          \\-----------------> observed | conflict

Only ``observed`` lets the consuming phase advance. A record lives for one
phase entry: the save that commits the phase drops the records, the entry
observation and the completion context together (D2.4).

The **entry observation** (:class:`EntryObservation`, D4.4) is what the
controller read before the phase's first launch, in a kind-neutral shape:
remote refs, the base revision, the marker-bearing objects of the
phase's identities, and the open PR headed at a ref, when the phase may
adopt one. It is the launch fence: a difference after the agent
returned is something published during the run (ADR 0004 §2.9).

The **completion context** (D4.6) is the validated result data the phase
consumes once its effects are observed, stored in the form its state field
takes. There is one closed schema per publishing phase; recovery completes
from it and never from a rendered payload or the run log.

Every value here is validated on load, and a value that fails is corruption
(:class:`~autoforge.errors.StateError`): never defaulted, clipped or
repaired from GitHub.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, NoReturn

from .claims import (
    FOLLOW_UP,
    IMPLEMENTATION,
    PROGRESS,
    REVIEW,
    MarkerKind,
    render_follow_up_marker,
    render_implementation_marker,
    render_progress_marker,
    render_review_marker,
    scan,
)
from .errors import ConfigurationError, ControlResultValidationError, StateError
from .redaction import MAX_GROWTH_FACTOR, redact
from .replan_txn import MARKER_RE as REPLAN_MARKER_RE
from .replan_txn import TRANSACTION_ID_RE, scan_replan_markers
from .result_parser import (
    FINDING_ID_RE,
    FIX_RESOLUTIONS,
    MAX_FINDING_ID_CHARS,
    MAX_FINDINGS_PER_REVIEW,
    MAX_FIX_RATIONALE_CHARS,
    MAX_RESOLUTIONS_PER_FIX,
    MAX_ROADMAP_SECTION_CHARS,
    MAX_URL_CHARS,
    REVIEW_PROSE_SECTIONS,
    Finding,
    ReviewResult,
    check_published_finding,
    markdown_code_span,
    published_markdown_problem,
    published_payload_problem,
    validate_follow_up_body,
    validate_follow_up_title,
    validate_pr_body,
    validate_pr_title,
    validate_progress_text,
    validate_review_section,
    validate_roadmap_section,
)
from .transitions import Phase
from .validation import parse_comment_url, parse_issue_url, parse_pr_url

# -- bounds ------------------------------------------------------------------------

# D4.3: at most two issues per effect, the ``MAX_CLOSE_ATTEMPTS`` precedent.
# A rebase (D5.5) consumes an attempt too.
MAX_EFFECT_ATTEMPTS = 2

# GitHub's limit for an issue, PR or comment body, and for a title, re-checked
# against the live API by #160 (D2.4). ``github.py`` refuses a write over
# either before sending it; the record refuses to hold one.
MAX_BODY_CHARS = 65536
MAX_TITLE_CHARS = 256

# D2.4: the largest plan is FIX, one push plus one follow-up effect per open
# finding.
MAX_EFFECTS_PER_PLAN = 1 + MAX_FINDINGS_PER_REVIEW

# D2.4's total bound, over every payload string of the plan and the
# completion context counted at its stored bound. ``json.dumps`` writes the
# state with ``ensure_ascii``, at most 12 bytes per character (a surrogate
# pair of ``\\uXXXX`` escapes), so the effect part of the state file stays
# under 12 MiB in the worst case, far below ``MAX_STATE_FILE_BYTES`` (64 MiB).
MAX_EFFECT_STATE_CHARS = 1 << 20

# The fixed separator of a body append (D5.5): base, separator, block.
APPEND_SEPARATOR = "\n\n"

# A conflict reason names objects and a rule; it is controller prose, redacted
# and clipped at the writer and bounded on load.
MAX_CONFLICT_REASON_CHARS = 4096

# The entry observation: a phase reads at most a few refs, and one object per
# identity (an open finding's follow-up, an earlier round's deferral, a PR).
MAX_OBSERVED_REFS = 4
MAX_OBSERVED_OBJECTS = 4 * MAX_FINDINGS_PER_REVIEW
MAX_OBSERVED_KEY_CHARS = 2048

_SHA_RE = re.compile(r"^[0-9a-f]{40}\Z")
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}\Z")
_REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+\Z")
_MAX_REPOSITORY_CHARS = 200
_MAX_REF_CHARS = 255
# What ``git check-ref-format`` refuses in a component, plus whitespace: the
# record holds a ref the controller derived, so anything outside this is
# corruption, not an exotic but valid branch name.
_REF_FORBIDDEN_RE = re.compile(r"[\x00-\x20\x7f~^:?*\[\\]|\.\.|@\{|//|/\.|\.lock(?:/|\Z)")
_TEXT_CONTROL_RE = re.compile(r"[\x00\x7f]")
_RATIONALE_CONTROL_RE = re.compile(r"(?![\n\t])[\x00-\x1f\x7f-\x9f\u2028\u2029]")


def sha256_text(text: str) -> str:
    """The SHA-256 hex digest of ``text`` as UTF-8 (a body base, an outside-markers body)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def compose_append(base: str, block: str) -> str:
    """The payload of a body append: the base, the fixed separator, the block (D5.5)."""
    return f"{base}{APPEND_SEPARATOR}{block}"


def progress_comment_body(progress: str, marker: str) -> str:
    """K8's payload: the validated progress text, a blank line, the marker (§2.5)."""
    return f"{progress}{APPEND_SEPARATOR}{marker}"


# The prose sections of a review comment, in rendered order, under their
# fixed headings (#162).
REVIEW_SECTION_HEADINGS: tuple[tuple[str, str], ...] = (
    ("spec", "Spec"),
    ("standards", "Standards"),
    ("assessment", "Assessment"),
    ("observations", "Observations"),
    ("verification", "Verification"),
    ("summary", "Summary"),
)


def _review_comment_parts(
    result: ReviewResult, head: str, base: str, merge_base: str
) -> tuple[str, str]:
    """The round's review comment up to its marker, and the marker (#162)."""
    lines = [
        f"# AI Code Review — Round {result.round}",
        "",
        f"Reviewed HEAD: {markdown_code_span(head)} against base {markdown_code_span(base)} "
        f"(merge base {markdown_code_span(merge_base)})",
        "",
        "## Findings",
    ]
    for finding in result.findings:
        heading = f"### {finding.id} [{finding.classification}]"
        if finding.location:
            heading += f" {markdown_code_span(finding.location)}"
        if finding.title:
            heading += f" — {finding.title}"
        # Every field is a block of its own, at column 0 between blank lines,
        # so GitHub reads it in the context it was judged in alone: a title's
        # unpaired backtick ends with its heading line, and a resolution's
        # fence opens where the resolution starts.
        lines.extend(["", heading, "", "Required resolution:", "", finding.required_resolution])
    if not result.findings:
        lines.extend(["", "None."])
    for key, heading in REVIEW_SECTION_HEADINGS:
        lines.extend(["", f"## {heading}", "", result.sections[key]])
    verdict = "YES" if result.needs_fix_round else "NO"
    lines.extend(["", f"Needs another fix round: {verdict}"])
    marker = render_review_marker(
        result.round,
        head,
        base,
        merge_base,
        result.needs_fix_round,
        [finding.id for finding in result.findings],
    )
    return "\n".join(lines), marker


def review_comment_body(result: ReviewResult, head: str, base: str, merge_base: str) -> str:
    """K4's payload (#162): the round's review comment, rendered from the validated result.

    The heading, the binding line (``head``, ``base`` and ``merge_base`` are
    the controller's binding of the round, never the reviewer's), the
    findings' layout, the needs-fix line and the marker are the controller's
    rendering; each finding's title, location and required resolution, and
    the reviewer's prose sections under their fixed headings, are blocks of
    their own. The marker's ``needs_fix_round`` and ``finding_ids`` and the
    needs-fix line come from the same result, so they cannot disagree with
    it or with each other.
    """
    text, marker = _review_comment_parts(result, head, base, merge_base)
    return f"{text}{APPEND_SEPARATOR}{marker}"


def review_comment_problem(
    result: ReviewResult, head: str, base: str, merge_base: str
) -> str | None:
    """Why the comment :func:`review_comment_body` renders may not be published, or ``None``.

    Each field passed the published-content policy alone
    (``check_published_finding``, ``validate_review_section``); the comment
    is judged as a whole too, because fields join (#162). The credential rule
    applies to the whole body (D8.3), and the mention rule to everything
    before the controller's marker (D8.2): one field's unclosed fence or raw
    HTML can make a later field's code text. Judged before the result is
    accepted and again when a saved plan is loaded, so recovery never posts
    a comment the result path would have refused.
    """
    text, marker = _review_comment_parts(result, head, base, merge_base)
    return published_payload_problem(
        "review comment", f"{text}{APPEND_SEPARATOR}{marker}"
    ) or published_markdown_problem("review comment", text)


def implementation_closing_block(issue_url: str) -> str:
    """What K2's body ends with and K3 appends: ``Closes #n``, a blank line, the marker (#161)."""
    number = parse_issue_url(issue_url).number
    return f"Closes #{number}{APPEND_SEPARATOR}{render_implementation_marker(issue_url)}"


def follow_up_reference(pr_url: str, finding_id: str) -> str:
    """The controller's line in a follow-up issue it creates: the PR and finding (#163)."""
    return f"Deferred from finding `{finding_id}` of {parse_pr_url(pr_url).canonical}."


def follow_up_issue_body(text: str, pr_url: str, finding_id: str) -> str:
    """K5's body: the agent's text, the controller's reference, then the finding's marker (#163)."""
    return (
        f"{text}{APPEND_SEPARATOR}{follow_up_reference(pr_url, finding_id)}"
        f"{APPEND_SEPARATOR}{render_follow_up_marker(pr_url, finding_id)}"
    )


# -- closed sets -------------------------------------------------------------------


class EffectKind(StrEnum):
    """The closed Wave 1 effect-kind set (D5.1). Adding one is a protocol bump."""

    PUSH = "push"  # K1
    IMPLEMENTATION_PR = "implementation_pr"  # K2
    ADOPT_PR = "adopt_pr"  # K3
    REVIEW_COMMENT = "review_comment"  # K4
    FOLLOW_UP_ISSUE = "follow_up_issue"  # K5
    FOLLOW_UP_APPEND = "follow_up_append"  # K6
    REPLACEMENT_PR = "replacement_pr"  # K7
    PROGRESS_COMMENT = "progress_comment"  # K8


class Stage(StrEnum):
    """D2.3: the closed stage set of a record."""

    INTENDED = "intended"  # validated, persisted, never issued
    ATTEMPTED = "attempted"  # the count is persisted before each issue
    OBSERVED = "observed"  # the read-back matched; terminal
    CONFLICT = "conflict"  # BLOCKED, naming the object


# The phases whose external writes are effects, and which kinds each may plan.
PUBLISHING_PHASES = frozenset(
    {
        Phase.ANALYZE_EXECUTE,
        Phase.REVIEW,
        Phase.FIX,
        Phase.REPLAN_REEXECUTE,
        Phase.UPDATE_EPIC,
    }
)
KIND_PHASES: Mapping[EffectKind, frozenset[Phase]] = {
    EffectKind.PUSH: frozenset({Phase.ANALYZE_EXECUTE, Phase.FIX, Phase.REPLAN_REEXECUTE}),
    EffectKind.IMPLEMENTATION_PR: frozenset({Phase.ANALYZE_EXECUTE}),
    EffectKind.ADOPT_PR: frozenset({Phase.ANALYZE_EXECUTE}),
    EffectKind.REVIEW_COMMENT: frozenset({Phase.REVIEW}),
    EffectKind.FOLLOW_UP_ISSUE: frozenset({Phase.FIX}),
    EffectKind.FOLLOW_UP_APPEND: frozenset({Phase.FIX}),
    EffectKind.REPLACEMENT_PR: frozenset({Phase.REPLAN_REEXECUTE}),
    EffectKind.PROGRESS_COMMENT: frozenset({Phase.UPDATE_EPIC}),
}

# D13.3: the contract a launch ran under, recorded in every pre-launch save of
# a publishing phase. A phase is controller-published once the running version
# performs its writes itself; #161-#164 add theirs as they land.
LABEL_NONE = ""
LABEL_AGENT_PUBLISHES = "agent_publishes"
LABEL_CONTROLLER_PUBLISHES = "controller_publishes"
LAUNCH_LABELS = frozenset({LABEL_NONE, LABEL_AGENT_PUBLISHES, LABEL_CONTROLLER_PUBLISHES})
CONTROLLER_PUBLISHED_PHASES = frozenset(
    {Phase.ANALYZE_EXECUTE, Phase.REVIEW, Phase.FIX, Phase.UPDATE_EPIC}
)


def launch_label_for(phase: Phase) -> str:
    """The label the pre-launch save of ``phase`` records (D13.3)."""
    if phase not in PUBLISHING_PHASES:
        return LABEL_NONE
    if phase in CONTROLLER_PUBLISHED_PHASES:
        return LABEL_CONTROLLER_PUBLISHES
    return LABEL_AGENT_PUBLISHES


def is_legacy_reentry(phase: Phase, attempt: int, label: str) -> bool:
    """D13.3: a resumed launch ran under the agent-publishing contract of a phase
    this version publishes itself, so its objects are pre-existing, once."""
    return attempt >= 1 and label == LABEL_AGENT_PUBLISHES and phase in CONTROLLER_PUBLISHED_PHASES


# -- field checks ------------------------------------------------------------------

Checker = Callable[[object, str], None]


def _fail(what: str, message: str) -> NoReturn:
    raise StateError(f"{what} {message}")


def _exact(raw: object, keys: Sequence[str], what: str, optional: Sequence[str] = ()) -> dict:
    if not isinstance(raw, dict):
        _fail(what, f"must be an object, got {type(raw).__name__}")
    unknown = sorted(str(k) for k in raw if k not in keys and k not in optional)
    if unknown:
        _fail(what, f"has unknown key(s) {unknown}")
    missing = [k for k in keys if k not in raw]
    if missing:
        _fail(what, f"is missing key(s) {missing}")
    return raw


def _str(value: object, what: str, max_chars: int) -> str:
    if not isinstance(value, str):
        _fail(what, f"must be a string, got {type(value).__name__}")
    if len(value) > max_chars:
        _fail(what, f"is {len(value)} characters, over its bound of {max_chars}")
    return value


def _int(value: object, what: str, minimum: int = 0, maximum: int | None = None) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        _fail(what, f"must be an integer, got {type(value).__name__}")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f">= {minimum}" if maximum is None else f"in [{minimum}, {maximum}]"
        _fail(what, f"must be {bound}, got {value}")
    return value


def _bool(value: object, what: str) -> bool:
    if not isinstance(value, bool):
        _fail(what, f"must be a boolean, got {type(value).__name__}")
    return value


def _sha(value: object, what: str) -> None:
    if not isinstance(value, str) or not _SHA_RE.match(value):
        _fail(what, "must be a full 40-character lowercase commit SHA")


def _opt_sha(value: object, what: str) -> None:
    if value is not None:
        _sha(value, what)


def _digest(value: object, what: str) -> None:
    if not isinstance(value, str) or not _DIGEST_RE.match(value):
        _fail(what, "must be a 64-character lowercase SHA-256 hex digest")


def _opt_digest(value: object, what: str) -> None:
    if value is not None:
        _digest(value, what)


def _repository(value: object, what: str) -> None:
    text = _str(value, what, _MAX_REPOSITORY_CHARS)
    if not _REPOSITORY_RE.match(text):
        _fail(what, "must be an <owner>/<repo> repository name")


def _branch(value: object, what: str) -> None:
    text = _str(value, what, _MAX_REF_CHARS)
    if (
        not text
        or text.startswith(("-", "/", "refs/"))
        or text.endswith(("/", "."))
        or _REF_FORBIDDEN_RE.search(text)
    ):
        _fail(what, "must be a plain branch name")


def _ref(value: object, what: str) -> None:
    text = _str(value, what, _MAX_REF_CHARS)
    if not text.startswith("refs/heads/"):
        _fail(what, "must be a full refs/heads/<branch> ref")
    _branch(text.removeprefix("refs/heads/"), what)


def _canonical(value: object, what: str, parse: Callable[[str], object]) -> str:
    text = _str(value, what, MAX_URL_CHARS)
    try:
        ref = parse(text)
    except ConfigurationError:
        _fail(what, "is not a GitHub URL of the expected kind")
    if getattr(ref, "canonical", None) != text:
        _fail(what, "must be stored in its canonical form")
    return text


def _issue_url(value: object, what: str) -> None:
    _canonical(value, what, parse_issue_url)


def _opt_issue_url(value: object, what: str) -> None:
    if value is not None:
        _issue_url(value, what)


def _pr_url(value: object, what: str) -> None:
    _canonical(value, what, parse_pr_url)


def _comment_url(value: object, what: str) -> None:
    _canonical(value, what, parse_comment_url)


def _object_url(value: object, what: str) -> None:
    """An issue, PR or comment URL: what an observed marker-bearing object is."""
    text = _str(value, what, MAX_URL_CHARS)
    parse: Callable[[str], object] = parse_comment_url if "#" in text else _parse_issue_or_pr
    _canonical(text, what, parse)


def _parse_issue_or_pr(url: str) -> object:
    try:
        return parse_issue_url(url)
    except ConfigurationError:
        return parse_pr_url(url)


def _published_text(max_chars: int) -> Checker:
    """Persisted payload text: bounded and redaction-invariant (D2.4, D8.3)."""

    def check(value: object, what: str) -> None:
        text = _str(value, what, max_chars)
        if _TEXT_CONTROL_RE.search(text):
            _fail(what, "carries a NUL or DEL character")
        if redact(text) != text:
            # Never quoted: the text is what the check refused to keep.
            _fail(what, "is not redaction-invariant; a payload never holds a credential")

    return check


def _title(value: object, what: str) -> None:
    _published_text(MAX_TITLE_CHARS)(value, what)
    assert isinstance(value, str)
    if not value.strip() or "\n" in value or "\r" in value:
        _fail(what, "must be one non-empty line")


def _marker_of(kind: MarkerKind) -> Checker:
    def check(value: object, what: str) -> None:
        text = _str(value, what, MAX_OBSERVED_KEY_CHARS)
        found = scan(kind, text)
        if found.defects or len(found.claims) != 1 or not kind.pattern.fullmatch(text):
            _fail(what, f"must be exactly one well-formed {kind.name} marker")

    return check


def _replan_marker(value: object, what: str) -> None:
    text = _str(value, what, MAX_OBSERVED_KEY_CHARS)
    found = scan_replan_markers(text)
    if found.malformed or len(found.attestations) != 1 or not REPLAN_MARKER_RE.fullmatch(text):
        _fail(what, "must be exactly one well-formed replan transaction marker")


def _marker_list(value: object, what: str) -> None:
    if not isinstance(value, list) or not value:
        _fail(what, "must be a non-empty list of ai-follow-up markers")
    if len(value) > MAX_FINDINGS_PER_REVIEW:
        _fail(what, f"holds {len(value)} markers, over {MAX_FINDINGS_PER_REVIEW}")
    for i, item in enumerate(value):
        _marker_of(FOLLOW_UP)(item, f"{what}[{i}]")
    if len(set(value)) != len(value):
        _fail(what, "repeats a marker")


def _absent(value: object, what: str) -> None:
    if value is not True:
        _fail(what, "must be true: a create's precondition is that no object has the identity")


def _watermark(value: object, what: str) -> None:
    _int(value, what)


def _opt_sha_value(value: object, what: str) -> None:
    _opt_sha(value, what)


def _number(value: object, what: str) -> None:
    _int(value, what, minimum=1)


# -- per-kind schemas ----------------------------------------------------------------


@dataclass(frozen=True)
class _KindSpec:
    identity: Mapping[str, Checker]
    target: Mapping[str, Checker]
    precondition: Mapping[str, Checker]
    payload: Mapping[str, Checker]
    observed: Mapping[str, Checker]
    completion: str


_BODY = _published_text(MAX_BODY_CHARS)

KIND_SPECS: Mapping[EffectKind, _KindSpec] = {
    EffectKind.PUSH: _KindSpec(
        identity={"repository": _repository, "ref": _ref, "candidate_sha": _sha},
        target={"repository": _repository, "ref": _ref},
        precondition={"expected_old": _opt_sha_value, "base_sha": _sha},
        payload={"sha": _sha},
        observed={"sha": _sha},
        completion="remote_ref_equals_candidate",
    ),
    EffectKind.IMPLEMENTATION_PR: _KindSpec(
        identity={
            "repository": _repository,
            "marker": _marker_of(IMPLEMENTATION),
            "head_branch": _branch,
        },
        target={"repository": _repository, "base": _branch, "head": _branch},
        precondition={"absent": _absent},
        payload={"title": _title, "body": _BODY},
        observed={"url": _pr_url, "number": _number},
        completion="one_pr_on_head_branch_matching_payload",
    ),
    EffectKind.ADOPT_PR: _KindSpec(
        identity={"pr_url": _pr_url, "marker": _marker_of(IMPLEMENTATION)},
        target={"pr_url": _pr_url},
        precondition={"base_sha256": _digest},
        payload={"body": _BODY, "block": _BODY},
        observed={"url": _pr_url},
        completion="body_equals_payload_marker_exactly_one",
    ),
    EffectKind.REVIEW_COMMENT: _KindSpec(
        identity={"pr_url": _pr_url, "marker": _marker_of(REVIEW)},
        target={"pr_url": _pr_url},
        precondition={"absent": _absent},
        payload={"body": _BODY},
        observed={"url": _comment_url},
        completion="round_comment_exactly_one_body_equals_payload",
    ),
    EffectKind.FOLLOW_UP_ISSUE: _KindSpec(
        identity={"repository": _repository, "marker": _marker_of(FOLLOW_UP)},
        target={"repository": _repository},
        precondition={"absent": _absent, "watermark": _watermark},
        payload={"title": _title, "body": _BODY},
        observed={"url": _issue_url, "number": _number},
        completion="one_issue_above_watermark_matching_payload",
    ),
    EffectKind.FOLLOW_UP_APPEND: _KindSpec(
        identity={"issue_url": _issue_url, "markers": _marker_list},
        target={"issue_url": _issue_url},
        precondition={"base_sha256": _digest},
        payload={"body": _BODY, "block": _BODY},
        observed={"url": _issue_url},
        completion="body_equals_payload_markers_exactly_one",
    ),
    EffectKind.REPLACEMENT_PR: _KindSpec(
        identity={
            "repository": _repository,
            "transaction_marker": _replan_marker,
            "head_branch": _branch,
        },
        target={"repository": _repository, "base": _branch, "head": _branch},
        precondition={"absent": _absent, "watermark": _watermark},
        payload={"title": _title, "body": _BODY},
        observed={"url": _pr_url, "number": _number},
        completion="replacement_bound_at_candidate_matching_payload",
    ),
    EffectKind.PROGRESS_COMMENT: _KindSpec(
        identity={"epic_url": _issue_url, "marker": _marker_of(PROGRESS)},
        target={"epic_url": _issue_url},
        precondition={"absent": _absent},
        payload={"body": _BODY},
        observed={"url": _comment_url},
        completion="progress_comment_exactly_one_body_equals_payload",
    ),
}


def _check_fields(raw: object, schema: Mapping[str, Checker], what: str) -> dict:
    data = _exact(raw, tuple(schema), what)
    for key, check in schema.items():
        check(data[key], f"{what}.{key}")
    return dict(data)


# -- the owner binding ---------------------------------------------------------------


def _same_url(a: str, b: str, parse: Callable[[str], Any]) -> bool:
    if not a or not b:
        return a == b
    try:
        return bool(parse(a).identity == parse(b).identity)
    except ConfigurationError:
        return False


@dataclass(frozen=True)
class Binding:
    """What persisted effect state must agree with: the state it is loaded with.

    ``phase`` is the state's phase. A record, an observation or a context
    names a publishing phase; it must be the state's, unless the state is
    ``BLOCKED`` or ``FAILED`` (a conflict, or a rejection at the bound,
    stopped that phase), and then every piece names the same phase.
    """

    run_id: str
    phase: Phase
    issue_url: str
    pr_url: str
    review_round: int
    transaction_id: str
    open_finding_ids: tuple[str, ...] = ()

    def check_phase(self, phase: Phase, what: str) -> None:
        if phase not in PUBLISHING_PHASES:
            _fail(what, f"names {phase.value}, which publishes nothing")
        if self.phase in (Phase.BLOCKED, Phase.FAILED):
            return
        if phase != self.phase:
            _fail(what, f"is bound to {phase.value} but the state is in {self.phase.value}")

    def check_issue(self, issue_url: str, what: str) -> None:
        if not _same_url(issue_url, self.issue_url, parse_issue_url):
            _fail(what, "is bound to another issue than the state's current issue")

    def check_pr(self, pr_url: str, what: str) -> None:
        if not _same_url(pr_url, self.pr_url, parse_pr_url):
            _fail(what, "is bound to another PR than the state's current PR")


@dataclass(frozen=True)
class EffectOwner:
    """D2.2: the run, phase, issue, PR and (REPLAN_REEXECUTE) transaction of a record."""

    run_id: str
    phase: Phase
    issue_url: str
    pr_url: str
    transaction_id: str

    _KEYS = ("run_id", "phase", "issue_url", "pr_url", "transaction_id")

    @classmethod
    def from_dict(cls, raw: object, what: str) -> EffectOwner:
        data = _exact(raw, cls._KEYS, what)
        run_id = _str(data["run_id"], f"{what}.run_id", 128)
        phase_text = _str(data["phase"], f"{what}.phase", 64)
        try:
            phase = Phase(phase_text)
        except ValueError:
            _fail(f"{what}.phase", f"is not a phase: {phase_text!r}")
        _issue_url(data["issue_url"], f"{what}.issue_url")
        pr_url = _str(data["pr_url"], f"{what}.pr_url", MAX_URL_CHARS)
        if pr_url:
            _pr_url(pr_url, f"{what}.pr_url")
        txn = _str(data["transaction_id"], f"{what}.transaction_id", 32)
        if phase == Phase.REPLAN_REEXECUTE:
            if not TRANSACTION_ID_RE.match(txn):
                _fail(f"{what}.transaction_id", "must be the 32-hex replan transaction id")
        elif txn:
            _fail(f"{what}.transaction_id", f"must be empty outside {Phase.REPLAN_REEXECUTE.value}")
        return cls(run_id, phase, data["issue_url"], pr_url, txn)

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "phase": self.phase.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "transaction_id": self.transaction_id,
        }

    def check(self, binding: Binding, what: str) -> None:
        if self.run_id != binding.run_id:
            _fail(what, "is bound to another run than the state's")
        binding.check_phase(self.phase, what)
        binding.check_issue(self.issue_url, what)
        binding.check_pr(self.pr_url, what)
        if self.transaction_id != binding.transaction_id and self.phase == Phase.REPLAN_REEXECUTE:
            _fail(what, "is bound to another replan transaction than the state's")


# -- the record ------------------------------------------------------------------------


@dataclass(frozen=True)
class EffectRecord:
    """One effect of one phase entry (D2.1, D2.2). Immutable: a stage change is a new record."""

    position: int
    kind: EffectKind
    owner: EffectOwner
    identity: dict
    target: dict
    precondition: dict
    payload: dict
    completion: str
    stage: Stage = Stage.INTENDED
    attempts: int = 0
    observed: dict | None = None
    reason: str = ""

    _KEYS = (
        "position",
        "kind",
        "owner",
        "identity",
        "target",
        "precondition",
        "payload",
        "completion",
        "stage",
        "attempts",
        "observed",
        "reason",
    )

    # -- construction -------------------------------------------------------------
    @classmethod
    def plan(
        cls,
        position: int,
        kind: EffectKind,
        owner: EffectOwner,
        *,
        identity: dict,
        target: dict,
        precondition: dict,
        payload: dict,
    ) -> EffectRecord:
        """A new ``intended`` record, validated exactly as it will be on load.

        A value that fails here is a controller bug or a payload the planner
        should have refused first (an oversized body, a credential); it is
        raised as :class:`StateError`, never persisted.
        """
        record = cls(
            position=position,
            kind=kind,
            owner=owner,
            identity=dict(identity),
            target=dict(target),
            precondition=dict(precondition),
            payload=dict(payload),
            completion=KIND_SPECS[kind].completion,
        )
        return cls.from_dict(record.to_dict())

    @classmethod
    def from_dict(cls, raw: object, what: str = "effect record") -> EffectRecord:
        data = _exact(raw, cls._KEYS, what)
        position = _int(data["position"], f"{what}.position", 0, MAX_EFFECTS_PER_PLAN - 1)
        what = f"effect record {position}"
        kind_text = _str(data["kind"], f"{what}.kind", 64)
        try:
            kind = EffectKind(kind_text)
        except ValueError:
            _fail(f"{what}.kind", f"is not a Wave 1 effect kind: {kind_text!r}")
        spec = KIND_SPECS[kind]
        owner = EffectOwner.from_dict(data["owner"], f"{what}.owner")
        if owner.phase not in KIND_PHASES[kind]:
            _fail(what, f"is a {kind.value} effect, which {owner.phase.value} never plans")
        stage_text = _str(data["stage"], f"{what}.stage", 32)
        try:
            stage = Stage(stage_text)
        except ValueError:
            _fail(f"{what}.stage", f"is not a stage: {stage_text!r}")
        attempts = _int(data["attempts"], f"{what}.attempts", 0, MAX_EFFECT_ATTEMPTS)
        if stage == Stage.INTENDED and attempts != 0:
            _fail(what, "is intended but records an issued attempt")
        if stage == Stage.ATTEMPTED and attempts < 1:
            _fail(what, "is attempted but records no attempt")
        completion = data["completion"]
        if completion != spec.completion:
            _fail(f"{what}.completion", f"must be {spec.completion!r} for a {kind.value} effect")
        observed = data["observed"]
        if stage == Stage.OBSERVED:
            observed = _check_fields(observed, spec.observed, f"{what}.observed")
        elif observed is not None:
            _fail(f"{what}.observed", f"must be null while the record is {stage.value}")
        reason = _str(data["reason"], f"{what}.reason", MAX_CONFLICT_REASON_CHARS)
        if (stage == Stage.CONFLICT) != bool(reason):
            _fail(f"{what}.reason", "is set exactly when the record is a conflict")
        record = cls(
            position=position,
            kind=kind,
            owner=owner,
            identity=_check_fields(data["identity"], spec.identity, f"{what}.identity"),
            target=_check_fields(data["target"], spec.target, f"{what}.target"),
            precondition=_check_fields(
                data["precondition"], spec.precondition, f"{what}.precondition"
            ),
            payload=_check_fields(data["payload"], spec.payload, f"{what}.payload"),
            completion=completion,
            stage=stage,
            attempts=attempts,
            observed=observed,
            reason=reason,
        )
        _CROSS_CHECKS[kind](record, what)
        return record

    def to_dict(self) -> dict:
        return {
            "position": self.position,
            "kind": self.kind.value,
            "owner": self.owner.to_dict(),
            "identity": dict(self.identity),
            "target": dict(self.target),
            "precondition": dict(self.precondition),
            "payload": dict(self.payload),
            "completion": self.completion,
            "stage": self.stage.value,
            "attempts": self.attempts,
            "observed": None if self.observed is None else dict(self.observed),
            "reason": self.reason,
        }

    # -- stage transitions (D2.3, D4.1, D5.5) ---------------------------------------
    @property
    def pending(self) -> bool:
        """Not yet terminal: reconciled before anything else (D4.2)."""
        return self.stage in (Stage.INTENDED, Stage.ATTEMPTED)

    @property
    def bound_left(self) -> bool:
        return self.attempts < MAX_EFFECT_ATTEMPTS

    def attempting(self) -> EffectRecord:
        """The record saved before an issue: ``attempted``, count incremented (D4.1)."""
        if not self.pending:
            raise StateError(f"effect record {self.position} is {self.stage.value}; not issued")
        if not self.bound_left:
            raise StateError(f"effect record {self.position} has used its {MAX_EFFECT_ATTEMPTS}")
        return replace(self, stage=Stage.ATTEMPTED, attempts=self.attempts + 1)

    def rebasing(self, body: str) -> EffectRecord:
        """D5.5: from ``intended`` only, the append over ``body``, saved before its issue."""
        if self.kind not in APPEND_KINDS or self.stage != Stage.INTENDED:
            raise StateError(f"effect record {self.position} cannot be rebased")
        payload = {**self.payload, "body": compose_append(body, self.payload["block"])}
        rebased = replace(
            self,
            payload=payload,
            precondition={**self.precondition, "base_sha256": sha256_text(body)},
        ).attempting()
        return EffectRecord.from_dict(rebased.to_dict())

    def observing(self, observed: dict) -> EffectRecord:
        if not self.pending:
            raise StateError(f"effect record {self.position} is {self.stage.value}; not observed")
        record = replace(self, stage=Stage.OBSERVED, observed=dict(observed))
        return EffectRecord.from_dict(record.to_dict())

    def conflicting(self, reason: str) -> EffectRecord:
        return replace(self, stage=Stage.CONFLICT, reason=bounded_reason(reason))

    def reopened(self) -> EffectRecord:
        """A ``conflict`` the operator resolved: reconciled again, as ``attempted``.

        ``unblock`` back into the phase keeps the records (D2.4: they belong
        to the phase entry). A conflict carries no promise that nothing was
        sent, so it is reconciled as a record whose issue may have landed,
        with its count kept: the bound of D4.3 still holds across an unblock.
        """
        if self.stage != Stage.CONFLICT:
            return self
        if self.attempts == 0:
            return replace(self, stage=Stage.INTENDED, reason="")
        return replace(self, stage=Stage.ATTEMPTED, reason="")

    # -- convenience ------------------------------------------------------------------
    @property
    def body(self) -> str:
        return str(self.payload.get("body", ""))

    @property
    def marker(self) -> str:
        return str(self.identity.get("marker", ""))

    def describe(self) -> str:
        """``<kind> effect <position> on <target>``: names the object, never the payload."""
        target = (
            self.target.get("epic_url")
            or self.target.get("pr_url")
            or self.target.get("issue_url")
            or self.target.get("ref")
            or self.target.get("repository")
        )
        return f"{self.kind.value} effect {self.position} on {target}"


APPEND_KINDS = frozenset({EffectKind.ADOPT_PR, EffectKind.FOLLOW_UP_APPEND})


def bounded_reason(reason: str) -> str:
    """A conflict reason as it may be persisted: redacted, then clipped at the bound."""
    text = redact(reason or "conflict").strip() or "conflict"
    if len(text) > MAX_CONFLICT_REASON_CHARS:
        text = text[: MAX_CONFLICT_REASON_CHARS - 1] + "\u2026"
    return text


# -- per-kind cross-field checks --------------------------------------------------------


def _same(record: EffectRecord, what: str, *pairs: tuple[object, object, str]) -> None:
    for a, b, name in pairs:
        if a != b:
            _fail(what, f"has a {name} that differs between its identity and its target")


def _marker_claim(kind: MarkerKind, marker: str):  # noqa: ANN202 - the claim type varies
    return scan(kind, marker).claims[0]


def _check_owner_issue_marker(record: EffectRecord, kind: MarkerKind, what: str) -> None:
    claim = _marker_claim(kind, record.identity["marker"])
    issue = getattr(claim, "issue", None)
    if issue is not None and not _same_url(
        issue.canonical, record.owner.issue_url, parse_issue_url
    ):
        _fail(what, "carries a marker for another issue than its owner's")
    pr = getattr(claim, "pr", None)
    if pr is not None and not _same_url(pr.canonical, record.owner.pr_url, parse_pr_url):
        _fail(what, "carries a marker for another PR than its owner's")


def _check_body_ends_with_marker(record: EffectRecord, marker: str, what: str) -> None:
    if not record.body.endswith(marker):
        _fail(what, "has a payload body that does not end with its identity's marker")


def _check_append(record: EffectRecord, markers: Sequence[str], what: str) -> None:
    block = record.payload["block"]
    tail = APPEND_SEPARATOR + block
    if not record.body.endswith(tail):
        _fail(what, "has a payload that is not its base, the separator and its block")
    base = record.body[: len(record.body) - len(tail)]
    if sha256_text(base) != record.precondition["base_sha256"]:
        _fail(what, "has a payload whose base does not match its recorded base digest")
    for marker in markers:
        if marker not in block:
            _fail(what, "has a block that does not carry its identity's marker")


def _cross_push(record: EffectRecord, what: str) -> None:
    _same(
        record,
        what,
        (record.identity["repository"], record.target["repository"], "repository"),
        (record.identity["ref"], record.target["ref"], "ref"),
    )
    if record.identity["candidate_sha"] != record.payload["sha"]:
        _fail(what, "pushes another SHA than its identity's candidate")
    if record.payload["sha"] == record.precondition["base_sha"]:
        _fail(what, "pushes the base itself; a candidate differs from its base")
    if record.observed is not None and record.observed["sha"] != record.payload["sha"]:
        _fail(what, "is observed at another SHA than its candidate")


def _owner_closing_block(record: EffectRecord, repository: str, what: str) -> str:
    """The closing block of the owner's issue, for a PR in ``repository``.

    ``Closes #n`` names issue ``n`` of the PR's own repository, so it is the
    owner's issue only when the PR is in that issue's repository.
    """
    if not parse_issue_url(record.owner.issue_url).same_repository(repository):
        _fail(what, "is in another repository than its owner's issue, which 'Closes #n' names")
    return implementation_closing_block(record.owner.issue_url)


def _cross_implementation_pr(record: EffectRecord, what: str) -> None:
    _same(
        record,
        what,
        (record.identity["repository"], record.target["repository"], "repository"),
        (record.identity["head_branch"], record.target["head"], "head branch"),
    )
    _check_owner_issue_marker(record, IMPLEMENTATION, what)
    _check_body_ends_with_marker(record, record.identity["marker"], what)
    # The payload is the title and body the parser accepted, the body then
    # followed by a blank line and the controller's closing block; both texts
    # get the parser's rules again, because a resumed create publishes them
    # with no agent result in between. The credential rule over the whole
    # body is the record's redaction invariance.
    tail = APPEND_SEPARATOR + _owner_closing_block(record, record.target["repository"], what)
    if not record.body.endswith(tail):
        _fail(
            what,
            "has a payload body that is not its text, a blank line and the closing block "
            "of its owner's issue",
        )
    text = record.body[: len(record.body) - len(tail)]
    for key, value, validate in (
        ("title", str(record.payload["title"]), validate_pr_title),
        ("body", text, validate_pr_body),
    ):
        if value != value.strip():
            _fail(what, f"has a PR {key} that is not in the parser's stored form")
        try:
            validate(value)
        except ControlResultValidationError as exc:
            _fail(what, f"has an invalid PR {key}: {exc}")


def _cross_adopt_pr(record: EffectRecord, what: str) -> None:
    _same(record, what, (record.identity["pr_url"], record.target["pr_url"], "PR"))
    _check_owner_issue_marker(record, IMPLEMENTATION, what)
    _check_append(record, (record.identity["marker"],), what)
    # The base is the adopted PR's own body, a human's text the controller
    # keeps as it is; only the block is the controller's, and it is exactly
    # the closing block of the owner's issue.
    repository = parse_pr_url(record.target["pr_url"]).repository
    if record.payload["block"] != _owner_closing_block(record, repository, what):
        _fail(what, "has a block that is not the closing block of its owner's issue")
    if record.observed is not None and record.observed["url"] != record.target["pr_url"]:
        _fail(what, "is observed on another PR than its target")


def _cross_review_comment(record: EffectRecord, what: str) -> None:
    _same(record, what, (record.identity["pr_url"], record.target["pr_url"], "PR"))
    if not _same_url(record.target["pr_url"], record.owner.pr_url, parse_pr_url):
        _fail(what, "targets another PR than its owner's")
    # The review marker names a revision, not a PR: the comment is bound to its
    # PR by where it is posted, which the two checks above pin to the owner's.
    claim = _marker_claim(REVIEW, record.identity["marker"])
    if claim.reviewed_base_ref is None or claim.reviewed_merge_base_sha is None:
        _fail(what, "carries a review marker without the base it reviewed, which no round binds")
    _check_body_ends_with_marker(record, record.identity["marker"], what)
    # The rest of the body is checked where its source is: the REVIEW
    # completion context saved with it must render exactly this body from
    # fields that pass the parser's rules (ReviewContext.from_dict).
    if record.observed is not None and not parse_comment_url(record.observed["url"]).on(
        parse_pr_url(record.target["pr_url"])
    ):
        _fail(what, "is observed on another PR than its target")


def _cross_follow_up_issue(record: EffectRecord, what: str) -> None:
    _same(record, what, (record.identity["repository"], record.target["repository"], "repo"))
    _check_owner_issue_marker(record, FOLLOW_UP, what)
    _check_body_ends_with_marker(record, record.identity["marker"], what)
    if not parse_pr_url(record.owner.pr_url).same_repository(record.target["repository"]):
        _fail(what, "creates a follow-up issue in another repository than its owner's PR")
    # The payload is the title and body the parser accepted, the body then
    # followed by the controller's reference to the PR and finding and the
    # marker (#163); both texts get the parser's rules again, because a
    # resumed create publishes them with no agent result in between.
    claim = _marker_claim(FOLLOW_UP, record.identity["marker"])
    tail = (
        f"{APPEND_SEPARATOR}{follow_up_reference(record.owner.pr_url, claim.finding_id)}"
        f"{APPEND_SEPARATOR}{record.identity['marker']}"
    )
    if not record.body.endswith(tail):
        _fail(
            what,
            "has a payload body that is not its text, the controller's reference to its PR "
            "and finding, and its marker",
        )
    text = record.body[: len(record.body) - len(tail)]
    for key, value, validate in (
        ("title", str(record.payload["title"]), validate_follow_up_title),
        ("body", text, validate_follow_up_body),
    ):
        if value != value.strip():
            _fail(what, f"has a follow-up {key} that is not in the parser's stored form")
        try:
            validate(value)
        except ControlResultValidationError as exc:
            _fail(what, f"has an invalid follow-up {key}: {exc}")


def _cross_follow_up_append(record: EffectRecord, what: str) -> None:
    _same(record, what, (record.identity["issue_url"], record.target["issue_url"], "issue"))
    for marker in record.identity["markers"]:
        claim = _marker_claim(FOLLOW_UP, marker)
        if not _same_url(claim.pr.canonical, record.owner.pr_url, parse_pr_url):
            _fail(what, "carries a follow-up marker for another PR than its owner's")
    if record.payload["block"] != "\n".join(record.identity["markers"]):
        _fail(what, "has a block that is not its markers, one per line in plan order")
    _check_append(record, record.identity["markers"], what)
    if _same_url(record.target["issue_url"], record.owner.issue_url, parse_issue_url):
        _fail(what, "appends to the current issue, which is never a follow-up")
    if record.observed is not None and record.observed["url"] != record.target["issue_url"]:
        _fail(what, "is observed on another issue than its target")


def _cross_replacement_pr(record: EffectRecord, what: str) -> None:
    _same(
        record,
        what,
        (record.identity["repository"], record.target["repository"], "repository"),
        (record.identity["head_branch"], record.target["head"], "head branch"),
    )
    attestation = scan_replan_markers(record.identity["transaction_marker"]).attestations[0]
    if attestation.transaction_id != record.owner.transaction_id:
        _fail(what, "carries the marker of another replan transaction than its owner's")
    if record.identity["transaction_marker"] not in record.body:
        _fail(what, "has a payload body that does not carry its transaction marker")


def _cross_progress_comment(record: EffectRecord, what: str) -> None:
    _same(record, what, (record.identity["epic_url"], record.target["epic_url"], "EPIC"))
    _check_owner_issue_marker(record, PROGRESS, what)
    _check_body_ends_with_marker(record, record.identity["marker"], what)
    # The payload is the progress text the parser accepted and the marker
    # (``progress_comment_body``); the text gets the parser's rules again,
    # because a resumed write publishes it with no agent result in between.
    tail = APPEND_SEPARATOR + record.identity["marker"]
    if not record.body.endswith(tail):
        _fail(what, "has a payload body that is not its progress text, a blank line and its marker")
    try:
        validate_progress_text(record.body[: len(record.body) - len(tail)])
    except ControlResultValidationError as exc:
        _fail(what, f"has an invalid progress text: {exc}")
    if record.observed is not None and not parse_comment_url(record.observed["url"]).on(
        parse_issue_url(record.target["epic_url"])
    ):
        _fail(what, "is observed on another issue than its EPIC")


_CROSS_CHECKS: Mapping[EffectKind, Callable[[EffectRecord, str], None]] = {
    EffectKind.PUSH: _cross_push,
    EffectKind.IMPLEMENTATION_PR: _cross_implementation_pr,
    EffectKind.ADOPT_PR: _cross_adopt_pr,
    EffectKind.REVIEW_COMMENT: _cross_review_comment,
    EffectKind.FOLLOW_UP_ISSUE: _cross_follow_up_issue,
    EffectKind.FOLLOW_UP_APPEND: _cross_follow_up_append,
    EffectKind.REPLACEMENT_PR: _cross_replacement_pr,
    EffectKind.PROGRESS_COMMENT: _cross_progress_comment,
}


def payload_chars(records: Sequence[EffectRecord]) -> int:
    """The payload characters D2.4's total bound counts for ``records``."""
    return sum(len(v) for r in records for v in r.payload.values() if isinstance(v, str))


def load_records(raw: object, binding: Binding) -> tuple[EffectRecord, ...]:
    """The persisted plan, validated: positions in order, one owner, D2.4's bounds."""
    if not isinstance(raw, list):
        _fail("effect_records", f"must be a list, got {type(raw).__name__}")
    if len(raw) > MAX_EFFECTS_PER_PLAN:
        _fail("effect_records", f"holds {len(raw)} records, over {MAX_EFFECTS_PER_PLAN}")
    records = tuple(
        EffectRecord.from_dict(item, f"effect_records[{i}]") for i, item in enumerate(raw)
    )
    for i, record in enumerate(records):
        if record.position != i:
            _fail(f"effect_records[{i}]", f"is at position {record.position}, not {i}")
        if record.owner != records[0].owner:
            _fail(f"effect_records[{i}]", "has another owner than the plan's first record")
        record.owner.check(binding, f"effect_records[{i}]")
    if sum(1 for r in records if r.kind == EffectKind.PUSH) > 1:
        _fail("effect_records", "plans more than one push")
    return records


# -- the entry observation (D4.4) -----------------------------------------------------


@dataclass(frozen=True)
class EntryObservation:
    """What a publishing phase read before its first launch, kind-neutral (D4.4).

    ``refs`` maps a ``refs/heads/...`` ref to its head SHA, or ``None`` when
    the ref was absent. ``objects`` maps a marker (the identity text) to the
    one object that carried it, or ``None`` when none did. ``prs`` maps a
    ref of ``refs`` whose PRs were read to the one open PR of the repository
    headed at it, or ``None`` when there was none: the PR a phase may adopt
    carries no marker yet, so it is recorded by its branch (#161, ADR 0004
    K3). A ref missing from ``prs`` had its PRs not read, which is not "no
    PR": a phase that needs the answer fails where it uses it. ``prs`` is
    stored only when it is not empty, so an observation that read no PR
    (every phase but ``ANALYZE_EXECUTE``) is stored as before it existed.
    """

    phase: Phase
    issue_url: str
    pr_url: str
    refs: Mapping[str, str | None]
    base_sha: str | None
    objects: Mapping[str, str | None]
    prs: Mapping[str, str | None] = field(default_factory=dict)

    _KEYS = ("phase", "issue_url", "pr_url", "refs", "base_sha", "objects")
    _OPTIONAL_KEYS = ("prs",)

    @classmethod
    def from_dict(cls, raw: object, binding: Binding) -> EntryObservation:
        what = "entry_observation"
        data = _exact(raw, cls._KEYS, what, cls._OPTIONAL_KEYS)
        phase_text = _str(data["phase"], f"{what}.phase", 64)
        try:
            phase = Phase(phase_text)
        except ValueError:
            _fail(f"{what}.phase", f"is not a phase: {phase_text!r}")
        binding.check_phase(phase, what)
        _issue_url(data["issue_url"], f"{what}.issue_url")
        binding.check_issue(data["issue_url"], what)
        pr_url = _str(data["pr_url"], f"{what}.pr_url", MAX_URL_CHARS)
        if pr_url:
            _pr_url(pr_url, f"{what}.pr_url")
        binding.check_pr(pr_url, what)
        refs = data["refs"]
        if not isinstance(refs, dict) or len(refs) > MAX_OBSERVED_REFS:
            _fail(f"{what}.refs", f"must be an object of at most {MAX_OBSERVED_REFS} refs")
        for ref, sha in refs.items():
            _ref(ref, f"{what}.refs key")
            _opt_sha(sha, f"{what}.refs[{ref}]")
        _opt_sha(data["base_sha"], f"{what}.base_sha")
        objects = data["objects"]
        if not isinstance(objects, dict) or len(objects) > MAX_OBSERVED_OBJECTS:
            _fail(f"{what}.objects", f"must be an object of at most {MAX_OBSERVED_OBJECTS}")
        for key, url in objects.items():
            _observed_identity(key, f"{what}.objects key")
            if url is not None:
                _object_url(url, f"{what}.objects value")
        prs = data.get("prs", {})
        if not isinstance(prs, dict) or len(prs) > MAX_OBSERVED_REFS or ("prs" in data and not prs):
            _fail(
                f"{what}.prs",
                f"must be a non-empty object of at most {MAX_OBSERVED_REFS} refs when stored",
            )
        for ref, url in prs.items():
            _ref(ref, f"{what}.prs key")
            if ref not in refs:
                _fail(f"{what}.prs", f"records a PR on {ref}, a ref the observation did not read")
            if url is not None:
                _pr_url(url, f"{what}.prs[{ref}]")
        return cls(
            phase,
            data["issue_url"],
            pr_url,
            dict(refs),
            data["base_sha"],
            dict(objects),
            dict(prs),
        )

    def to_dict(self) -> dict:
        data = {
            "phase": self.phase.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "refs": dict(self.refs),
            "base_sha": self.base_sha,
            "objects": dict(self.objects),
        }
        if self.prs:
            data["prs"] = dict(self.prs)
        return data


def _observed_identity(value: object, what: str) -> None:
    text = _str(value, what, MAX_OBSERVED_KEY_CHARS)
    for kind in (IMPLEMENTATION, REVIEW, PROGRESS, FOLLOW_UP):
        if kind.pattern.fullmatch(text):
            _marker_of(kind)(text, what)
            return
    if REPLAN_MARKER_RE.fullmatch(text):
        _replan_marker(text, what)
        return
    _fail(what, "must be one controller marker (an identity)")


# -- completion contexts (D4.6) -------------------------------------------------------

# Per-key overhead counted for each stored value: the key, quotes and
# punctuation of its JSON form, generously.
_KEY_OVERHEAD = 64


def _context_header(raw: object, keys: Sequence[str], binding: Binding, phase: Phase) -> dict:
    what = f"{phase.value} completion context"
    data = _exact(raw, ("phase", *keys), what)
    if data["phase"] != phase.value:
        _fail(f"{what}.phase", f"must be {phase.value}")
    binding.check_phase(phase, what)
    return data


@dataclass(frozen=True)
class AnalyzeContext:
    """``ANALYZE_EXECUTE``: no result data; completion persists K1's and K2's/K3's results.

    The plan saved with it is the push (K1) at position 0, then either the
    implementation PR (K2) on the pushed branch or the adoption (K3), and the
    push is the one the entry observation explains: its base is the
    observed default-branch head and its expected old value the observed
    head of its ref (#161). So is the PR: the observation read the PRs on
    the pushed ref, a create follows an observation of none there onto the
    default branch the observation read (its ref, at the base), and an
    adoption targets the very PR it recorded (ADR 0004 K2, K3), so a PR
    that appeared or changed after the entry is never adopted from a plan.
    """

    issue_url: str

    PHASE = Phase.ANALYZE_EXECUTE
    STORED_BOUND = MAX_URL_CHARS + 2 * _KEY_OVERHEAD

    @classmethod
    def from_dict(
        cls,
        raw: object,
        binding: Binding,
        *,
        records: Sequence[EffectRecord] = (),
        observation: EntryObservation | None = None,
        **_: object,
    ) -> AnalyzeContext:
        what = "ANALYZE_EXECUTE completion context"
        data = _context_header(raw, ("issue_url",), binding, cls.PHASE)
        _issue_url(data["issue_url"], f"{what}.issue_url")
        binding.check_issue(data["issue_url"], what)
        kinds = [r.kind for r in records]
        if kinds not in (
            [EffectKind.PUSH, EffectKind.IMPLEMENTATION_PR],
            [EffectKind.PUSH, EffectKind.ADOPT_PR],
        ):
            _fail(what, "must be saved with a push then a PR create or adoption")
        if observation is None:
            _fail(what, "must be saved with the entry observation it is checked against")
        push = records[0]
        ref = push.target["ref"]
        if ref not in observation.refs:
            _fail(what, "pushes a ref the entry observation did not read")
        if push.precondition["expected_old"] != observation.refs[ref]:
            _fail(what, "pushes over another head than the entry observed on its ref")
        if push.precondition["base_sha"] != observation.base_sha:
            _fail(what, "pushes a candidate checked against another base than the entry read")
        second = records[1]
        if second.kind is EffectKind.IMPLEMENTATION_PR and (
            "refs/heads/" + str(second.target["head"]) != ref
        ):
            _fail(what, "opens its PR from another branch than the one it pushes")
        if second.kind is EffectKind.IMPLEMENTATION_PR:
            base_ref = "refs/heads/" + str(second.target["base"])
            if (
                base_ref == ref
                or base_ref not in observation.refs
                or observation.refs[base_ref] != observation.base_sha
            ):
                _fail(
                    what, "opens its PR onto another branch than the default branch the entry read"
                )
        if ref not in observation.prs:
            _fail(what, "is saved with an entry observation that did not read the PRs on its ref")
        observed_pr = observation.prs[ref]
        if second.kind is EffectKind.IMPLEMENTATION_PR and observed_pr is not None:
            _fail(what, "opens a PR on a branch where the entry observed an open PR")
        if second.kind is EffectKind.ADOPT_PR and (
            observed_pr is None
            or not _same_url(str(second.target["pr_url"]), observed_pr, parse_pr_url)
        ):
            _fail(what, "adopts another PR than the one the entry observed on its branch")
        return cls(data["issue_url"])

    def to_dict(self) -> dict:
        return {"phase": self.PHASE.value, "issue_url": self.issue_url}


@dataclass(frozen=True)
class ReviewContext:
    """``REVIEW``: the round, ``needs_fix_round``, the findings and the prose sections.

    It is saved with the round's review comment (K4) alone, and the
    comment's marker is the context's verdict: the same round, the same
    ``needs_fix_round`` and the findings' ids in the same order (#162).
    The comment's body is exactly what :func:`review_comment_body` renders
    from the context at the revision the marker binds, and passes
    :func:`review_comment_problem` as a whole, so every published field of a
    body that recovery posts, and their composition, was validated under the
    parser's rules when the state was loaded, not only when the result was
    accepted.
    """

    issue_url: str
    pr_url: str
    round: int
    needs_fix_round: bool
    findings: tuple[dict, ...]
    sections: dict[str, str]

    PHASE = Phase.REVIEW
    # A finding's stored form is bounded by its parser fields.
    _FINDING_BOUND = 2000 + 200 + 300 + MAX_FINDING_ID_CHARS + 16 + 5 * _KEY_OVERHEAD
    _SECTIONS_BOUND = (
        sum(limit + _KEY_OVERHEAD for _, limit in REVIEW_PROSE_SECTIONS) + _KEY_OVERHEAD
    )
    STORED_BOUND = (
        2 * MAX_URL_CHARS
        + MAX_FINDINGS_PER_REVIEW * _FINDING_BOUND
        + _SECTIONS_BOUND
        + 7 * _KEY_OVERHEAD
    )

    @classmethod
    def from_dict(
        cls,
        raw: object,
        binding: Binding,
        *,
        records: Sequence[EffectRecord] = (),
        **_: object,
    ) -> ReviewContext:
        what = "REVIEW completion context"
        data = _context_header(
            raw,
            ("issue_url", "pr_url", "round", "needs_fix_round", "findings", "sections"),
            binding,
            cls.PHASE,
        )
        _issue_url(data["issue_url"], f"{what}.issue_url")
        binding.check_issue(data["issue_url"], what)
        _pr_url(data["pr_url"], f"{what}.pr_url")
        binding.check_pr(data["pr_url"], what)
        round_ = _int(data["round"], f"{what}.round", minimum=1)
        if round_ != binding.review_round + 1:
            _fail(
                f"{what}.round",
                f"is {round_} but the round under review is {binding.review_round + 1}",
            )
        needs_fix = _bool(data["needs_fix_round"], f"{what}.needs_fix_round")
        raw_findings = data["findings"]
        if not isinstance(raw_findings, list) or len(raw_findings) > MAX_FINDINGS_PER_REVIEW:
            _fail(f"{what}.findings", f"must be a list of at most {MAX_FINDINGS_PER_REVIEW}")
        parsed_findings: list[Finding] = []
        findings: list[dict] = []
        for i, item in enumerate(raw_findings):
            try:
                finding = Finding.from_payload(item, round_, i)
                # The findings were accepted as publishable in the round's comment.
                check_published_finding(finding)
            except ControlResultValidationError as exc:
                _fail(f"{what}.findings[{i}]", f"is invalid: {exc}")
            parsed = finding.to_dict()
            if parsed != item:
                _fail(f"{what}.findings[{i}]", "is not in the parser's stored form")
            parsed_findings.append(finding)
            findings.append(parsed)
        if len({f["id"] for f in findings}) != len(findings):
            _fail(f"{what}.findings", "repeats a finding id")
        if needs_fix != bool(findings):
            _fail(f"{what}.needs_fix_round", "must be true exactly when findings are present")
        sections = cls._sections(data["sections"], f"{what}.sections")
        if [r.kind for r in records] != [EffectKind.REVIEW_COMMENT]:
            _fail(what, "must be saved with exactly one review comment")
        record = records[0]
        claim = _marker_claim(REVIEW, record.identity["marker"])
        if (
            claim.round != round_
            or claim.needs_fix_round != needs_fix
            or claim.finding_ids != tuple(f["id"] for f in findings)
        ):
            _fail(what, "disagrees with its review comment's marker on the round's verdict")
        # A resumed create posts the body with no reviewer result in between
        # (D4.6), so the body must be the controller's rendering of what was
        # just validated, at the revision its marker binds: nothing else in it
        # is the reviewer's, and nothing in it escaped the parser's rules.
        # The record's own check has already refused a marker without a base.
        result = ReviewResult(
            round=round_,
            reviewed_head_sha=claim.reviewed_head_sha,
            needs_fix_round=needs_fix,
            findings=parsed_findings,
            sections=sections,
        )
        binding_args = (
            claim.reviewed_head_sha,
            claim.reviewed_base_ref or "",
            claim.reviewed_merge_base_sha or "",
        )
        if record.body != review_comment_body(result, *binding_args):
            _fail(
                what,
                "is saved with a review comment whose body is not the comment the controller "
                "renders from it",
            )
        # Fields valid one by one can still compose a comment the result path
        # refuses (an unclosed fence closed by a later field's); such a plan
        # was never accepted, so it is refused, never replayed.
        problem = review_comment_problem(result, *binding_args)
        if problem:
            _fail(what, f"is saved with a review comment the controller may not publish: {problem}")
        return cls(data["issue_url"], data["pr_url"], round_, needs_fix, tuple(findings), sections)

    @staticmethod
    def _sections(raw: object, what: str) -> dict[str, str]:
        """The prose sections, each in the stored form the parser returns for it."""
        data = _exact(raw, tuple(key for key, _ in REVIEW_PROSE_SECTIONS), what)
        sections: dict[str, str] = {}
        for key, _ in REVIEW_PROSE_SECTIONS:
            try:
                parsed = validate_review_section(key, data[key])
            except ControlResultValidationError as exc:
                _fail(f"{what}.{key}", f"is invalid: {exc}")
            if parsed != data[key]:
                _fail(f"{what}.{key}", "is not in the parser's stored form")
            sections[key] = parsed
        return sections

    def to_dict(self) -> dict:
        return {
            "phase": self.PHASE.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "round": self.round,
            "needs_fix_round": self.needs_fix_round,
            "findings": [dict(f) for f in self.findings],
            "sections": dict(self.sections),
        }


FOLLOW_UP_SOURCE_ENTRY = "entry"
# D4.6: the redacted form of a FIX rationale, at most this long.
MAX_STORED_RATIONALE_CHARS = MAX_FIX_RATIONALE_CHARS * MAX_GROWTH_FACTOR


@dataclass(frozen=True)
class FixContext:
    """``FIX``: one resolution per open finding, rationales in their redacted form."""

    issue_url: str
    pr_url: str
    round: int
    resolutions: tuple[dict, ...]

    PHASE = Phase.FIX
    _RESOLUTION_KEYS = ("finding_id", "resolution", "rationale", "commit_sha", "follow_up_source")
    _RESOLUTION_BOUND = (
        MAX_STORED_RATIONALE_CHARS + MAX_FINDING_ID_CHARS + 40 + 32 + 16 + (5 * _KEY_OVERHEAD)
    )
    STORED_BOUND = (
        2 * MAX_URL_CHARS + MAX_RESOLUTIONS_PER_FIX * _RESOLUTION_BOUND + (5 * _KEY_OVERHEAD)
    )

    @classmethod
    def from_dict(
        cls,
        raw: object,
        binding: Binding,
        *,
        records: Sequence[EffectRecord] = (),
        observation: EntryObservation | None = None,
        **_: object,
    ) -> FixContext:
        what = "FIX completion context"
        data = _context_header(
            raw, ("issue_url", "pr_url", "round", "resolutions"), binding, cls.PHASE
        )
        _issue_url(data["issue_url"], f"{what}.issue_url")
        binding.check_issue(data["issue_url"], what)
        _pr_url(data["pr_url"], f"{what}.pr_url")
        binding.check_pr(data["pr_url"], what)
        round_ = _int(data["round"], f"{what}.round", minimum=1)
        if round_ != binding.review_round:
            _fail(
                f"{what}.round", f"is {round_} but the round being fixed is {binding.review_round}"
            )
        raw_resolutions = data["resolutions"]
        if not isinstance(raw_resolutions, list) or len(raw_resolutions) > MAX_RESOLUTIONS_PER_FIX:
            _fail(f"{what}.resolutions", f"must be a list of at most {MAX_RESOLUTIONS_PER_FIX}")
        resolutions = [
            cls._resolution(item, f"{what}.resolutions[{i}]")
            for i, item in enumerate(raw_resolutions)
        ]
        ids = [r["finding_id"] for r in resolutions]
        if len(set(ids)) != len(ids):
            _fail(f"{what}.resolutions", "resolves a finding twice")
        if sorted(ids) != sorted(binding.open_finding_ids):
            _fail(f"{what}.resolutions", "must resolve exactly the open findings")
        cls._check_sources(resolutions, data["pr_url"], records, observation, what)
        return cls(data["issue_url"], data["pr_url"], round_, tuple(resolutions))

    @classmethod
    def _resolution(cls, raw: object, what: str) -> dict:
        data = _exact(raw, cls._RESOLUTION_KEYS, what)
        fid = _str(data["finding_id"], f"{what}.finding_id", MAX_FINDING_ID_CHARS)
        if not FINDING_ID_RE.match(fid):
            _fail(f"{what}.finding_id", "is not a finding id")
        resolution = data["resolution"]
        if resolution not in FIX_RESOLUTIONS:
            _fail(f"{what}.resolution", f"must be one of {FIX_RESOLUTIONS}")
        rationale = _str(data["rationale"], f"{what}.rationale", MAX_STORED_RATIONALE_CHARS)
        if _RATIONALE_CONTROL_RE.search(rationale):
            _fail(f"{what}.rationale", "carries a control character other than a newline or a tab")
        commit = _str(data["commit_sha"], f"{what}.commit_sha", 40)
        if commit:
            _sha(commit, f"{what}.commit_sha")
        source = data["follow_up_source"]
        if source is not None and source != FOLLOW_UP_SOURCE_ENTRY:
            _int(source, f"{what}.follow_up_source", 0, MAX_EFFECTS_PER_PLAN - 1)
        if (resolution == "follow_up_created") != (source is not None):
            _fail(f"{what}.follow_up_source", "is set exactly when the finding is deferred")
        return dict(data)

    @staticmethod
    def _check_sources(
        resolutions: Sequence[dict],
        pr_url: str,
        records: Sequence[EffectRecord],
        observation: EntryObservation | None,
        what: str,
    ) -> None:
        """D4.6: each deferred finding has exactly one source, fixed at the plan."""
        from .claims import render_follow_up_marker

        observed = dict(observation.objects) if observation is not None else {}
        own: dict[str, bool] = {}
        for resolution in resolutions:
            marker = render_follow_up_marker(pr_url, resolution["finding_id"])
            own[marker] = observed.get(marker) is not None
        block_markers = {
            m
            for r in records
            if r.kind == EffectKind.FOLLOW_UP_APPEND
            for m in r.identity["markers"]
        }
        for resolution in resolutions:
            fid = resolution["finding_id"]
            marker = render_follow_up_marker(pr_url, fid)
            source = resolution["follow_up_source"]
            label = f"{what} resolution for {fid}"
            if own[marker]:
                if source != FOLLOW_UP_SOURCE_ENTRY:
                    _fail(label, "must reuse the finding's own follow-up through 'entry'")
                if marker in block_markers:
                    _fail(label, "is the marker of a reused follow-up, which no block carries")
                continue
            if source == FOLLOW_UP_SOURCE_ENTRY:
                _fail(label, "names 'entry' but the entry observation holds no follow-up for it")
            if source is None:
                continue
            assert isinstance(source, int)
            if source >= len(records):
                _fail(label, f"names plan position {source}, which the plan does not hold")
            record = records[source]
            if record.kind == EffectKind.FOLLOW_UP_ISSUE:
                if record.identity["marker"] != marker:
                    _fail(label, f"names follow-up effect {source}, which is another finding's")
            elif record.kind == EffectKind.FOLLOW_UP_APPEND:
                if marker not in record.identity["markers"]:
                    _fail(label, f"names append effect {source}, whose block lacks its marker")
            else:
                _fail(label, f"names effect {source}, which is not a follow-up effect")

    def to_dict(self) -> dict:
        return {
            "phase": self.PHASE.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "round": self.round,
            "resolutions": [dict(r) for r in self.resolutions],
        }


@dataclass(frozen=True)
class ReplanContext:
    """``REPLAN_REEXECUTE``: the counts the K7 marker renders."""

    issue_url: str
    pr_url: str
    transaction_id: str
    historical_findings_considered: int
    unique_failure_constraints: int

    PHASE = Phase.REPLAN_REEXECUTE
    STORED_BOUND = 2 * MAX_URL_CHARS + 32 + 2 * 20 + 6 * _KEY_OVERHEAD

    @classmethod
    def from_dict(cls, raw: object, binding: Binding, **_: object) -> ReplanContext:
        what = "REPLAN_REEXECUTE completion context"
        data = _context_header(
            raw,
            (
                "issue_url",
                "pr_url",
                "transaction_id",
                "historical_findings_considered",
                "unique_failure_constraints",
            ),
            binding,
            cls.PHASE,
        )
        _issue_url(data["issue_url"], f"{what}.issue_url")
        binding.check_issue(data["issue_url"], what)
        _pr_url(data["pr_url"], f"{what}.pr_url")
        binding.check_pr(data["pr_url"], what)
        txn = _str(data["transaction_id"], f"{what}.transaction_id", 32)
        if not TRANSACTION_ID_RE.match(txn) or txn != binding.transaction_id:
            _fail(f"{what}.transaction_id", "is not the state's replan transaction id")
        return cls(
            data["issue_url"],
            data["pr_url"],
            txn,
            _int(data["historical_findings_considered"], f"{what}.historical_findings_considered"),
            _int(data["unique_failure_constraints"], f"{what}.unique_failure_constraints"),
        )

    def to_dict(self) -> dict:
        return {
            "phase": self.PHASE.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "transaction_id": self.transaction_id,
            "historical_findings_considered": self.historical_findings_considered,
            "unique_failure_constraints": self.unique_failure_constraints,
        }


@dataclass(frozen=True)
class UpdateEpicContext:
    """``UPDATE_EPIC``: the roadmap section, the selection, and two digests (D4.6).

    ``roadmap_section`` is the section when one is required, else ``None``.
    ``next_issue_url`` is the validated selection, or ``None`` for "the EPIC
    is complete". The digests are SHA-256 of the EPIC body outside the
    roadmap markers: as the entry read before the result's launch saw it,
    and as the splice of this section into that body leaves it. They are
    present exactly when the section is.

    A re-request (D4.7) starts from a persisted rejection: ``selection_void``
    or ``section_void`` marks the input the rejection voided, and a void
    input is ``None``.
    """

    issue_url: str
    pr_url: str
    roadmap_section: str | None
    next_issue_url: str | None
    entry_outside_sha256: str | None
    spliced_outside_sha256: str | None
    selection_void: bool = False
    section_void: bool = False

    PHASE = Phase.UPDATE_EPIC
    _KEYS = (
        "issue_url",
        "pr_url",
        "roadmap_section",
        "next_issue_url",
        "entry_outside_sha256",
        "spliced_outside_sha256",
        "selection_void",
        "section_void",
    )
    STORED_BOUND = MAX_ROADMAP_SECTION_CHARS + 3 * MAX_URL_CHARS + 2 * 64 + 9 * _KEY_OVERHEAD

    @classmethod
    def from_dict(
        cls,
        raw: object,
        binding: Binding,
        *,
        records: Sequence[EffectRecord] = (),
        observation: EntryObservation | None = None,
        **_: object,
    ) -> UpdateEpicContext:
        what = "UPDATE_EPIC completion context"
        data = _context_header(raw, cls._KEYS, binding, cls.PHASE)
        _issue_url(data["issue_url"], f"{what}.issue_url")
        binding.check_issue(data["issue_url"], what)
        _pr_url(data["pr_url"], f"{what}.pr_url")
        binding.check_pr(data["pr_url"], what)
        section = data["roadmap_section"]
        if section is not None:
            _str(section, f"{what}.roadmap_section", MAX_ROADMAP_SECTION_CHARS)
            if not section.strip():
                _fail(f"{what}.roadmap_section", "must be non-empty when present")
            if redact(section) != section:
                _fail(f"{what}.roadmap_section", "is not redaction-invariant")
            # A parsed-form value gets its parser's rules again (D4.6): the
            # recovery that completes from this context writes the section
            # into the EPIC body with no agent result in between.
            try:
                validate_roadmap_section(section)
            except ControlResultValidationError as exc:
                _fail(f"{what}.roadmap_section", f"is invalid: {exc}")
        _opt_issue_url(data["next_issue_url"], f"{what}.next_issue_url")
        _opt_digest(data["entry_outside_sha256"], f"{what}.entry_outside_sha256")
        _opt_digest(data["spliced_outside_sha256"], f"{what}.spliced_outside_sha256")
        has_digests = data["entry_outside_sha256"] is not None
        if (data["spliced_outside_sha256"] is not None) != has_digests:
            _fail(what, "must hold both outside-markers digests or neither")
        if has_digests != (section is not None):
            _fail(what, "holds the outside-markers digests exactly when it holds a section")
        selection_void = _bool(data["selection_void"], f"{what}.selection_void")
        section_void = _bool(data["section_void"], f"{what}.section_void")
        if selection_void and data["next_issue_url"] is not None:
            _fail(what, "voids its selection but still holds one")
        if section_void and section is not None:
            _fail(what, "voids its roadmap section but still holds one")
        cls._check_publication(data["issue_url"], data["pr_url"], records, observation, what)
        return cls(
            data["issue_url"],
            data["pr_url"],
            section,
            data["next_issue_url"],
            data["entry_outside_sha256"],
            data["spliced_outside_sha256"],
            selection_void,
            section_void,
        )

    @staticmethod
    def _check_publication(
        issue_url: str,
        pr_url: str,
        records: Sequence[EffectRecord],
        observation: EntryObservation | None,
        what: str,
    ) -> None:
        """The progress comment is planned or adopted, never neither (D2.2, D4.6, D13.7).

        Completion publishes only what the plan holds, so an empty plan beside
        this context would finish the phase with no progress comment. The
        entry observation decides which plan is consistent: no comment
        carried the marker of (issue, PR) at entry, and the plan is exactly
        that comment's K8 record; or one did, the entry adopted it (a legacy
        re-entry, D13.7), and the plan is empty.
        """
        if observation is None:
            _fail(what, "is persisted without the entry observation read before its launch")
        marker = render_progress_marker(issue_url, pr_url)
        if marker not in observation.objects:
            _fail(what, "has an entry observation that does not record its progress marker")
        adopted = observation.objects[marker]
        if adopted is not None:
            if records:
                _fail(what, f"plans a progress comment beside the adopted {adopted}")
            return
        if (
            len(records) != 1
            or records[0].kind != EffectKind.PROGRESS_COMMENT
            or records[0].identity["marker"] != marker
        ):
            _fail(
                what,
                "must be saved with the one progress-comment record of its marker: the entry "
                "observation records no comment to adopt",
            )

    def to_dict(self) -> dict:
        return {
            "phase": self.PHASE.value,
            "issue_url": self.issue_url,
            "pr_url": self.pr_url,
            "roadmap_section": self.roadmap_section,
            "next_issue_url": self.next_issue_url,
            "entry_outside_sha256": self.entry_outside_sha256,
            "spliced_outside_sha256": self.spliced_outside_sha256,
            "selection_void": self.selection_void,
            "section_void": self.section_void,
        }

    @property
    def void(self) -> bool:
        return self.selection_void or self.section_void


CompletionContext = AnalyzeContext | ReviewContext | FixContext | ReplanContext | UpdateEpicContext

_CONTEXT_TYPES: Mapping[str, Any] = {
    Phase.ANALYZE_EXECUTE.value: AnalyzeContext,
    Phase.REVIEW.value: ReviewContext,
    Phase.FIX.value: FixContext,
    Phase.REPLAN_REEXECUTE.value: ReplanContext,
    Phase.UPDATE_EPIC.value: UpdateEpicContext,
}


def _context_type(raw: dict) -> Any:
    """The context class of the publishing phase ``raw`` names, or ``None``."""
    phase = raw.get("phase")
    return _CONTEXT_TYPES.get(phase) if isinstance(phase, str) else None


def load_context(
    raw: object,
    binding: Binding,
    *,
    records: Sequence[EffectRecord] = (),
    observation: EntryObservation | None = None,
) -> CompletionContext:
    """The persisted completion context of the phase it names, strictly validated."""
    if not isinstance(raw, dict):
        _fail("completion_context", f"must be an object, got {type(raw).__name__}")
    context_type = _context_type(raw)
    if context_type is None:
        _fail("completion_context.phase", "must name a publishing phase")
    context: CompletionContext = context_type.from_dict(
        raw, binding, records=records, observation=observation
    )
    return context


def plan_size_problem(records: Sequence[EffectRecord], context: CompletionContext | None) -> str:
    """D2.4's total bound, or "" when the plan fits: payloads plus the context's stored bound."""
    total = payload_chars(records) + (type(context).STORED_BOUND if context is not None else 0)
    if total > MAX_EFFECT_STATE_CHARS:
        return (
            f"the effect plan holds {total} characters with its completion context, over the "
            f"bound of {MAX_EFFECT_STATE_CHARS}"
        )
    return ""


@dataclass(frozen=True)
class PhaseEffects:
    """The effect state of one phase entry, as loaded: plan, observation, context."""

    records: tuple[EffectRecord, ...] = ()
    observation: EntryObservation | None = None
    context: CompletionContext | None = None

    @property
    def phase(self) -> Phase | None:
        for piece in (self.context, self.observation):
            if piece is not None:
                return piece.PHASE if hasattr(piece, "PHASE") else piece.phase  # type: ignore[union-attr]
        return self.records[0].owner.phase if self.records else None

    @property
    def empty(self) -> bool:
        return not self.records and self.observation is None and self.context is None

    @property
    def published(self) -> bool:
        """D4.7: every record of the plan is observed (an empty plan is published)."""
        return all(r.stage == Stage.OBSERVED for r in self.records)


def load_phase_effects(
    records_raw: object, observation_raw: object, context_raw: object, binding: Binding
) -> PhaseEffects:
    """Validate the three persisted pieces against the state and against each other."""
    records = load_records(records_raw, binding)
    observation = None
    if not isinstance(observation_raw, dict):
        _fail("entry_observation", f"must be an object, got {type(observation_raw).__name__}")
    if observation_raw:
        observation = EntryObservation.from_dict(observation_raw, binding)
    if not isinstance(context_raw, dict):
        _fail("completion_context", f"must be an object, got {type(context_raw).__name__}")
    # One phase first: a context is validated against the plan and the
    # observation of its own phase, never against another phase's.
    context_type = _context_type(context_raw)
    phases = {
        p
        for p in (
            context_type.PHASE if context_type is not None else None,
            observation.phase if observation is not None else None,
            records[0].owner.phase if records else None,
        )
        if p is not None
    }
    if len(phases) > 1:
        _fail("effect state", "names more than one phase across records, observation and context")
    context = None
    if context_raw:
        context = load_context(context_raw, binding, records=records, observation=observation)
    effects = PhaseEffects(records, observation, context)
    if records and context is None:
        _fail("effect_records", "are persisted without the completion context saved with them")
    problem = plan_size_problem(records, context)
    if problem:
        _fail("effect state", problem)
    return effects


# -- reconciliation decisions (D4.2, D5.5) -------------------------------------------


class Action(StrEnum):
    OBSERVE = "observe"  # the target is in the intended end state
    ISSUE = "issue"  # save attempted (count + 1), then issue the persisted payload once
    REBASE = "rebase"  # D5.5: save the rebased payload as attempted, then issue it
    CONFLICT = "conflict"  # BLOCKED, naming the object
    DONE = "done"  # already observed: terminal, nothing to do


@dataclass(frozen=True)
class Decision:
    action: Action
    observed: dict | None = None  # OBSERVE: the observed result
    reason: str = ""  # CONFLICT: names the objects and the rule, never a payload
    rebase_body: str = ""  # REBASE: the body read now, the new base
    # CONFLICT only because the bound is spent and the write is still absent:
    # right after an issue that is "not visible yet", not yet a conflict.
    exhausted: bool = False


@dataclass(frozen=True)
class Found:
    """One remote object that carries a create effect's identity, as read back.

    ``in_end_state`` is the kind's completion predicate, evaluated by the
    caller's read (open, at the candidate, title and body equal to the
    payload, ...). ``observed`` is what the record would hold.
    """

    url: str
    in_end_state: bool
    observed: Mapping[str, object]


def _exhausted(record: EffectRecord) -> Decision:
    return Decision(
        Action.CONFLICT,
        exhausted=True,
        reason=(
            f"{record.describe()} has used its {MAX_EFFECT_ATTEMPTS} attempts and its outcome "
            "is still not on GitHub; perform the write by hand or remove the conflicting "
            "object, then 'unblock'"
        ),
    )


def _terminal(record: EffectRecord) -> Decision | None:
    if record.stage == Stage.OBSERVED:
        return Decision(Action.DONE, observed=dict(record.observed or {}))
    if record.stage == Stage.CONFLICT:
        return Decision(Action.CONFLICT, reason=record.reason)
    return None


def decide_create(record: EffectRecord, found: Sequence[Found]) -> Decision:
    """D4.2 for a create (K2, K4, K5, K7, K8) over a *complete* identity read.

    ``found`` is every object that carries the identity, in every state the
    kind's read covers (D4.5). One in the intended end state is the write;
    none means the precondition still holds; anything else is a conflict
    naming every object found.
    """
    terminal = _terminal(record)
    if terminal is not None:
        return terminal
    if len(found) == 1 and found[0].in_end_state:
        return Decision(Action.OBSERVE, observed=dict(found[0].observed))
    if not found:
        if record.bound_left:
            return Decision(Action.ISSUE)
        return _exhausted(record)
    urls = ", ".join(f.url for f in found)
    if len(found) == 1:
        return Decision(
            Action.CONFLICT,
            reason=(
                f"{record.describe()}: {urls} carries its identity but is not the object the "
                "controller intended (another body, title, head or state); it is never "
                "repaired in place or duplicated"
            ),
        )
    return Decision(
        Action.CONFLICT,
        reason=f"{record.describe()}: {len(found)} objects carry its identity ({urls})",
    )


def decide_append(
    record: EffectRecord,
    body_now: str,
    *,
    target_ok: bool,
    carries_block_marker: bool,
    rebase_problem: Callable[[str], str],
) -> Decision:
    """D5.5's table for a body append (K3, K6).

    ``target_ok`` is the kind's other conditions (open, in this repository,
    the K1 candidate as head, ...). ``carries_block_marker`` says whether
    ``body_now`` holds any marker of the block. ``rebase_problem`` judges a
    rebased payload (size and the credential check) and returns "" when it
    may be persisted; its reason names the pattern class only.
    """
    terminal = _terminal(record)
    if terminal is not None:
        return terminal
    if body_now == record.body:
        if target_ok:
            return Decision(Action.OBSERVE, observed={"url": _append_target(record)})
        return Decision(
            Action.CONFLICT,
            reason=f"{record.describe()}: the body is written but the target no longer qualifies",
        )
    if not target_ok:
        return Decision(
            Action.CONFLICT,
            reason=f"{record.describe()}: the target no longer meets the kind's conditions",
        )
    if sha256_text(body_now) == record.precondition["base_sha256"]:
        return Decision(Action.ISSUE) if record.bound_left else _exhausted(record)
    if record.stage == Stage.INTENDED and not carries_block_marker:
        problem = rebase_problem(compose_append(body_now, record.payload["block"]))
        if not problem:
            return Decision(Action.REBASE, rebase_body=body_now)
        return Decision(Action.CONFLICT, reason=f"{record.describe()}: no rebase: {problem}")
    if record.stage == Stage.INTENDED:
        why = "the body already carries a marker of the block but is not the payload"
    else:
        why = (
            "after an issue the body equals neither the payload nor its base; a write that "
            "landed and was edited cannot be told from one that never landed"
        )
    return Decision(Action.CONFLICT, reason=f"{record.describe()}: {why}")


def _append_target(record: EffectRecord) -> str:
    return str(record.target.get("pr_url") or record.target.get("issue_url"))


def decide_push(record: EffectRecord, remote_head: str | None) -> Decision:
    """K1's windows: the ref at the candidate is observed, at the expected old value is
    pushed (within the bound), anywhere else is a conflict naming both SHAs."""
    terminal = _terminal(record)
    if terminal is not None:
        return terminal
    candidate = record.payload["sha"]
    expected = record.precondition["expected_old"]
    ref = record.target["ref"]
    if remote_head == candidate:
        return Decision(Action.OBSERVE, observed={"sha": candidate})
    if remote_head == expected:
        return Decision(Action.ISSUE) if record.bound_left else _exhausted(record)
    return Decision(
        Action.CONFLICT,
        reason=(
            f"{record.describe()}: {ref} is at {remote_head or 'nothing'}, neither the "
            f"expected {expected or 'absent ref'} nor the candidate {candidate}"
        ),
    )
