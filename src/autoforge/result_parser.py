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
resolution without the evidence its kind requires, is rejected outright.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .errors import ConfigurationError, ControlResultError, ControlResultValidationError
from .transitions import Phase
from .validation import parse_comment_url, parse_issue_url, parse_pr_url

BEGIN = "<<<CONTROL_RESULT>>>"
END = "<<<END_CONTROL_RESULT>>>"

_BLOCK_RE = re.compile(r"<<<CONTROL_RESULT>>>(.*?)<<<END_CONTROL_RESULT>>>", re.DOTALL)
_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")
_FINDING_ID_RE = re.compile(r"^R(?P<round>[1-9][0-9]*)-F(?P<n>[1-9][0-9]*)$")

ALLOWED_STATUSES = ("success", "failure", "blocked")
FINDING_CLASSIFICATIONS = ("blocked", "non-blocked", "nit")
FIX_RESOLUTIONS = ("fixed", "follow_up_created", "no_change_with_rationale")
MIN_RATIONALE_CHARS = 40


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


def parse_control_result(stdout: str, expected_phase: Phase) -> dict:
    """Extract + JSON-parse + validate the CONTROL_RESULT payload.

    Returns the payload dict. Raises ControlResultError on extraction/JSON
    problems and ControlResultValidationError on schema/phase problems.
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
        validate_for_phase(expected_phase, payload)
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
        fid = _req_str(raw, "id", ph)
        m = _FINDING_ID_RE.match(fid)
        if not m:
            raise ControlResultValidationError(
                f"{ph}: finding id {fid!r} must look like R<round>-F<n> (e.g. R{round}-F1)"
            )
        if int(m.group("round")) != round:
            raise ControlResultValidationError(
                f"{ph}: finding id {fid!r} does not belong to review round {round}"
            )
        cls_ = _req_str(raw, "classification", ph)
        if cls_ not in FINDING_CLASSIFICATIONS:
            raise ControlResultValidationError(
                f"{ph}: finding {fid} classification must be one of "
                f"{FINDING_CLASSIFICATIONS}, got {cls_!r}"
            )
        return cls(
            id=fid,
            classification=cls_,
            required_resolution=_req_str(raw, "required_resolution", ph),
            title=str(raw.get("title", "") or ""),
            location=str(raw.get("location", "") or ""),
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
        raw_findings = p.get("findings")
        if not isinstance(raw_findings, list):
            raise ControlResultValidationError("'findings' must be a list (empty when clean)")
        findings = [Finding.from_payload(f, rnd, i) for i, f in enumerate(raw_findings)]
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
        fid = _req_str(raw, "finding_id", ph)
        if not _FINDING_ID_RE.match(fid):
            raise ControlResultValidationError(f"{ph}: invalid finding_id {fid!r}")
        res = _req_str(raw, "resolution", ph)
        if res not in FIX_RESOLUTIONS:
            raise ControlResultValidationError(
                f"{ph}: resolution for {fid} must be one of {FIX_RESOLUTIONS}, got {res!r}"
            )
        rationale = str(raw.get("rationale", "") or "").strip()
        follow_up = str(raw.get("follow_up_issue_url", "") or "").strip()
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
            commit_sha=str(raw.get("commit_sha", "") or ""),
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
        if parse_pr_url(previous_pr).canonical == parse_pr_url(replacement_pr).canonical:
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


def validate_for_phase(phase: Phase, payload: dict) -> None:
    """Enforce the per-phase required-fields schema (raises on violation)."""
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
