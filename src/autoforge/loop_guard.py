"""Loop bounds for the REVIEW <-> FIX cycle (pure, easily tested logic).

The engine records one entry per *completed* review round of the current PR
in ``state.review_history`` (see :func:`review_record`) and asks these
functions whether the loop must stop:

- :func:`round_cap_reason` — the hard cap ``workflow.max_review_rounds``.
- :func:`stagnation_reason` — consecutive rounds with findings that show no
  progress: identical required resolutions (normalised text) for
  ``workflow.stagnation_identical_rounds`` rounds, or an unchanged finding
  count for ``workflow.stagnation_unchanged_count_rounds`` rounds. A clean
  or stale round breaks the streak.
- :func:`step_budget_reason` — the cumulative ``workflow.max_total_steps``
  budget, measured on the persisted ``step_count`` so ``resume`` continues
  the same budget instead of starting a new one.

Every function returns a human-readable reason (non-empty -> BLOCKED) or
``""``. Nothing here reads GitHub or invokes an agent.
"""

from __future__ import annotations

import hashlib
import re

_WS_RE = re.compile(r"\s+")
MAX_PERSISTED_FINDINGS_PER_ROUND = 100
MAX_REQUIRED_RESOLUTION_CHARS = 2000

# ``result`` values recorded per review round.
RESULT_NEEDS_FIX = "needs_fix"
RESULT_CLEAN = "clean"
RESULT_STALE = "stale"


def normalize_resolution(text: object) -> str:
    """Whitespace-collapsed, lower-cased ``required_resolution`` text."""
    return _WS_RE.sub(" ", str(text or "")).strip().lower()


def findings_fingerprint(findings: list[dict]) -> str:
    """Order-independent digest of the findings' normalised required resolutions.

    Finding IDs (``R1-F1``, ``R2-F1``) change every round and are ignored;
    two rounds fingerprint the same when they request the same resolutions.
    """
    texts = sorted(normalize_resolution(f.get("required_resolution")) for f in findings)
    digest = hashlib.sha256("\n".join(texts).encode("utf-8")).hexdigest()
    return digest[:16]


def review_record(
    round: int,
    reviewed_head_sha: str,
    result: str,
    findings: list[dict],
    review_comment_url: str = "",
    timestamp: str = "",
) -> dict:
    """One ``review_history`` entry (plain dict: it is persisted as JSON)."""
    if result not in (RESULT_NEEDS_FIX, RESULT_CLEAN, RESULT_STALE):
        raise ValueError(f"unknown review result {result!r}")
    record = {
        "round": int(round),
        "reviewed_head_sha": reviewed_head_sha,
        "result": result,
        "finding_count": len(findings),
        "fingerprint": findings_fingerprint(findings),
    }
    # Keep compact, bounded evidence. GitHub remains authoritative for comment
    # bodies and the full finding list if this history is truncated.
    if findings:
        record["findings"] = [
            {
                "id": str(f.get("id", "")),
                "classification": str(f.get("classification", "")),
                "required_resolution": str(f.get("required_resolution", ""))[
                    :MAX_REQUIRED_RESOLUTION_CHARS
                ],
            }
            for f in findings[:MAX_PERSISTED_FINDINGS_PER_ROUND]
        ]
    if review_comment_url:
        record["review_comment_url"] = review_comment_url
    if timestamp:
        record["timestamp"] = timestamp
    return record


def round_cap_reason(review_round: int, max_review_rounds: int, *, has_findings: bool) -> str:
    """Reason when review round ``review_round`` (just completed) hits the cap.

    A round *at* the cap that still has findings must not start another FIX
    (its result could never be reviewed); a round *past* the cap must never
    have run at all. A clean round at the cap is fine (``""``).
    """
    if review_round > max_review_rounds:
        return f"review round {review_round} exceeds workflow.max_review_rounds={max_review_rounds}"
    if has_findings and review_round >= max_review_rounds:
        return (
            f"review round {review_round} still has findings and workflow.max_review_rounds="
            f"{max_review_rounds} is reached; no further FIX round is started"
        )
    return ""


def next_round_cap_reason(completed_rounds: int, max_review_rounds: int) -> str:
    """Reason when review round ``completed_rounds + 1`` must not start."""
    if completed_rounds >= max_review_rounds:
        return (
            f"{completed_rounds} review round(s) completed for this PR and "
            f"workflow.max_review_rounds={max_review_rounds} is reached; review round "
            f"{completed_rounds + 1} is not started"
        )
    return ""


def _trailing_needs_fix(history: list[dict], window: int) -> list[dict]:
    """The last ``window`` entries when all of them are needs_fix rounds, else []."""
    if window < 1 or len(history) < window:
        return []
    tail = history[-window:]
    if any(r.get("result") != RESULT_NEEDS_FIX for r in tail):
        return []
    return tail


def stagnation_reason(
    history: list[dict], identical_rounds: int, unchanged_count_rounds: int
) -> str:
    """Reason when the trailing review rounds show no progress; ``""`` otherwise.

    ``identical_rounds`` / ``unchanged_count_rounds`` of 0 disable the
    respective rule. Only *consecutive* rounds that ended with findings are
    considered: a clean or stale round in between resets both rules.
    """
    tail = _trailing_needs_fix(history, identical_rounds)
    if tail and len({r.get("fingerprint") for r in tail}) == 1:
        rounds = ", ".join(str(r.get("round")) for r in tail)
        return (
            f"review rounds {rounds} requested identical resolutions "
            f"({tail[-1].get('finding_count')} finding(s) each); the FIX rounds between them "
            f"changed nothing the reviewer accepts (workflow.stagnation_identical_rounds="
            f"{identical_rounds})"
        )
    tail = _trailing_needs_fix(history, unchanged_count_rounds)
    if tail and len({r.get("finding_count") for r in tail}) == 1:
        rounds = ", ".join(str(r.get("round")) for r in tail)
        return (
            f"review rounds {rounds} each ended with {tail[-1].get('finding_count')} "
            f"finding(s); the finding count has not changed "
            f"(workflow.stagnation_unchanged_count_rounds={unchanged_count_rounds})"
        )
    return ""


def step_budget_reason(step_count: int, max_total_steps: int) -> str:
    """Reason when the run's cumulative step budget is exhausted."""
    if step_count >= max_total_steps:
        return (
            f"{step_count} step(s) executed in this run and workflow.max_total_steps="
            f"{max_total_steps} is reached; no further step is executed"
        )
    return ""
