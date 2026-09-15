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
can re-emit it.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .errors import ConfigurationError, ControlResultError, ControlResultValidationError
from .transitions import Phase, WorkflowMode
from .validation import parse_comment_url, parse_issue_url, parse_pr_url

BEGIN = "<<<CONTROL_RESULT>>>"
END = "<<<END_CONTROL_RESULT>>>"

_BLOCK_RE = re.compile(r"<<<CONTROL_RESULT>>>(.*?)<<<END_CONTROL_RESULT>>>", re.DOTALL)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FINDING_ID_RE = re.compile(r"^R(?P<round>[1-9][0-9]*)-F(?P<n>[1-9][0-9]*)$")
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
    stdout: str, expected_phase: Phase, mode: WorkflowMode = WorkflowMode.REMOTE
) -> dict:
    """Extract + JSON-parse + validate the CONTROL_RESULT payload.

    Returns the payload dict. Raises ControlResultError on extraction/JSON
    problems and ControlResultValidationError on schema/phase problems.
    ``mode`` selects the per-phase schema: a LOCAL phase reports semantic
    facts (a summary, a workspace fingerprint, finding resolutions) and never
    a PR URL, a comment URL or a follow-up Issue.
    """
    raw = extract_last_block(stdout)
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
        validate_for_phase(expected_phase, payload, mode)
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


def _req_sha(payload: dict, key: str, phase: str) -> str:
    v = _req_str(payload, key, phase)
    if not _SHA_RE.match(v):
        raise ControlResultValidationError(
            f"{phase}: field {key!r} must be a git SHA (7-40 hex chars), got {v!r}"
        )
    return v.lower()


def _req_url(payload: dict, key: str, phase: str, kind: str) -> str:
    v = _req_str(payload, key, phase)
    parser = {"issue": parse_issue_url, "pr": parse_pr_url, "comment": parse_comment_url}[kind]
    try:
        parser(v)
    except ConfigurationError as exc:
        raise ControlResultValidationError(
            f"{phase}: field {key!r} must be a GitHub {kind} URL: {exc}"
        ) from exc
    return v


def _req_bool(payload: dict, key: str, phase: str) -> bool:
    if key not in payload:
        raise ControlResultValidationError(
            f"CONTROL_RESULT for {phase} missing required field {key!r}"
        )
    v = payload[key]
    if not isinstance(v, bool):
        raise ControlResultValidationError(f"{phase}: field {key!r} must be a boolean")
    return v


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


def _bounded(text: str, fid: str, key: str, limit: int) -> str:
    """Reject a finding text field longer than ``limit`` characters.

    The message reports the size, never the text: the oversized value is the
    thing being refused, and the error is echoed into the correction prompt
    and the run log.
    """
    if len(text) > limit:
        raise ControlResultValidationError(
            f"REVIEW: finding {fid} field {key!r} is {len(text)} characters; the controller "
            f"accepts at most {limit}. Shorten it (or split the finding) and re-emit the "
            "CONTROL_RESULT."
        )
    return text


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
        cls_ = _req_str(raw, "classification", ph)
        if cls_ not in FINDING_CLASSIFICATIONS:
            raise ControlResultValidationError(
                f"{ph}: finding {fid} classification must be one of "
                f"{FINDING_CLASSIFICATIONS}, got {cls_!r}"
            )
        return cls(
            id=fid,
            classification=cls_,
            required_resolution=_bounded(
                _req_str(raw, "required_resolution", ph),
                fid,
                "required_resolution",
                MAX_FINDING_RESOLUTION_CHARS,
            ),
            title=_bounded(_opt_str(raw, "title", ph), fid, "title", MAX_FINDING_TITLE_CHARS),
            location=_bounded(
                _opt_str(raw, "location", ph), fid, "location", MAX_FINDING_LOCATION_CHARS
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
        rationale = _opt_str(raw, "rationale", ph)
        follow_up = _opt_str(raw, "follow_up_issue_url", ph)
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
            commit_sha=_opt_str(raw, "commit_sha", ph),
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
        raw = p.get("resolutions")
        if not isinstance(raw, list):
            raise ControlResultValidationError("'resolutions' must be a list (one per finding)")
        resolutions = [FindingResolution.from_payload(r, i) for i, r in enumerate(raw)]
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


@dataclass
class UpdateEpicResult:
    next_issue_url: str | None

    @classmethod
    def from_payload(cls, p: dict) -> UpdateEpicResult:
        if "next_issue_url" not in p:
            raise ControlResultValidationError(
                "CONTROL_RESULT for UPDATE_EPIC missing required field "
                "'next_issue_url' (use null when the epic is complete)"
            )
        nxt = p["next_issue_url"]
        if nxt is not None and not isinstance(nxt, str):
            raise ControlResultValidationError("'next_issue_url' must be a string or null")
        if isinstance(nxt, str) and nxt == "":
            nxt = None
        return cls(next_issue_url=nxt)


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
        rationale = _opt_str(raw, "rationale", ph)
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
        raw = p.get("resolutions")
        if not isinstance(raw, list):
            raise ControlResultValidationError("'resolutions' must be a list (one per finding)")
        resolutions = [LocalFindingResolution.from_payload(r, i) for i, r in enumerate(raw)]
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
    phase: Phase, payload: dict, mode: WorkflowMode = WorkflowMode.REMOTE
) -> None:
    """Enforce the per-phase required-fields schema (raises on violation)."""
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
        UpdateEpicResult.from_payload(payload)
    elif phase in NON_AGENT_PHASES:
        raise ControlResultValidationError(
            f"phase {phase.value} is executed by the controller and never accepts an "
            "agent CONTROL_RESULT"
        )
