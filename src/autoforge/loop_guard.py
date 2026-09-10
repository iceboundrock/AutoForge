"""Loop bounds for the REVIEW <-> FIX cycle (pure, easily tested logic).

The engine records one entry per *completed* review round of the current PR
in ``state.review_history`` (see :func:`review_record`) and asks these
functions whether the loop must stop:

- :func:`round_cap_reason` — the hard cap ``workflow.max_review_rounds``.
- :func:`stagnation_reason` — consecutive rounds with findings that show no
  progress: identical required resolutions (normalised text) for
  ``workflow.stagnation_identical_rounds`` rounds, or an unchanged finding
  count for ``workflow.stagnation_unchanged_count_rounds`` rounds *while at
  least one required resolution recurs inside that window* (the same demand
  keeps coming back, e.g. an A/B/A ping-pong). Rounds whose findings are all
  new are progress, not stagnation, however many of them there are: a picky
  reviewer that raises one fresh finding per round is bounded by the hard
  cap only. A clean or stale round breaks the streak. Both rules compare
  rounds *against each other*, so a window of 1 has no meaning and the
  configuration rejects it: the settings are 0 (disabled) or >= 2.
- :func:`step_budget_reason` — the cumulative ``workflow.max_total_steps``
  budget, measured on the persisted ``step_count`` so ``resume`` continues
  the same budget instead of starting a new one.

Every function returns a human-readable reason (non-empty -> BLOCKED) or
``""``. Nothing here reads GitHub or invokes an agent.
"""

from __future__ import annotations

import hashlib
import re

from .errors import StateError

_WS_RE = re.compile(r"\s+")

# ``result`` values recorded per review round.
RESULT_NEEDS_FIX = "needs_fix"
RESULT_CLEAN = "clean"
RESULT_STALE = "stale"
RESULTS = (RESULT_NEEDS_FIX, RESULT_CLEAN, RESULT_STALE)

# Per-round bound on the persisted recurrence evidence. A review round may
# return arbitrarily many findings, and ``review_history`` keeps one entry per
# round for the whole PR, so an unbounded digest list would let a single
# verbose reviewer grow the state file without limit. A clipped round is
# marked ``resolutions_truncated`` and can then no longer prove the *absence*
# of a recurrence (see :func:`_recurring_resolutions`).
MAX_PERSISTED_RESOLUTION_DIGESTS = 50


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


def resolution_digests(findings: list[dict]) -> list[str]:
    """Sorted per-finding digests of the normalised required resolutions.

    Unlike :func:`findings_fingerprint` (one digest per round) this keeps one
    digest per finding, so a later round can be checked for *recurring*
    demands without persisting the review text itself.

    A resolution that normalises to the empty string demands nothing, so it
    is not recurrence evidence and gets no digest: otherwise two rounds that
    each carry a blank ``required_resolution`` would look like the same
    demand coming back.
    """
    digests = {
        hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        for text in (normalize_resolution(f.get("required_resolution")) for f in findings)
        if text
    }
    return sorted(digests)


def review_record(round: int, reviewed_head_sha: str, result: str, findings: list[dict]) -> dict:
    """One ``review_history`` entry (plain dict: it is persisted as JSON).

    The digest list is clipped to ``MAX_PERSISTED_RESOLUTION_DIGESTS``; a
    clipped entry carries ``resolutions_truncated: True`` so the stagnation
    rule knows its recurrence evidence is incomplete.
    """
    if result not in RESULTS:
        raise ValueError(f"unknown review result {result!r}")
    digests = resolution_digests(findings)
    return {
        "round": int(round),
        "reviewed_head_sha": reviewed_head_sha,
        "result": result,
        "finding_count": len(findings),
        "fingerprint": findings_fingerprint(findings),
        "resolutions": digests[:MAX_PERSISTED_RESOLUTION_DIGESTS],
        "resolutions_truncated": len(digests) > MAX_PERSISTED_RESOLUTION_DIGESTS,
    }


def validate_review_history(history: object) -> None:
    """Raise :class:`StateError` unless ``history`` is a list of valid entries.

    Persisted ``review_history`` drives a terminal decision (BLOCKED), so a
    malformed entry must fail loudly instead of being reinterpreted. Only a
    *missing* ``resolutions`` key is compatibility (an entry written before
    per-finding digests existed); a present field is validated like any other.
    """
    if not isinstance(history, list):
        raise StateError("'review_history' must be a list")
    for i, entry in enumerate(history):
        where = f"'review_history[{i}]'"
        if not isinstance(entry, dict):
            raise StateError(f"{where} must be an object")
        for key, typ in (("round", int), ("finding_count", int)):
            value = entry.get(key)
            if not isinstance(value, typ) or isinstance(value, bool):
                raise StateError(f"{where}.{key} must be an integer, got {value!r}")
        for key in ("reviewed_head_sha", "fingerprint"):
            if not isinstance(entry.get(key), str):
                raise StateError(f"{where}.{key} must be a string, got {entry.get(key)!r}")
        if entry.get("result") not in RESULTS:
            raise StateError(
                f"{where}.result must be one of {RESULTS}, got {entry.get('result')!r}"
            )
        if "resolutions" in entry:
            digests = entry["resolutions"]
            if not isinstance(digests, list):
                raise StateError(
                    f"{where}.resolutions must be a list of digest strings, got {digests!r}"
                )
            for d in digests:
                if not isinstance(d, str) or not d.strip():
                    raise StateError(
                        f"{where}.resolutions must contain non-empty digest strings, got {d!r}"
                    )
        if "resolutions_truncated" in entry and not isinstance(
            entry["resolutions_truncated"], bool
        ):
            raise StateError(
                f"{where}.resolutions_truncated must be a boolean, "
                f"got {entry['resolutions_truncated']!r}"
            )


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


def _recurring_resolutions(tail: list[dict]) -> tuple[set[str], str]:
    """``(digests requested by more than one round, incomplete-evidence reason)``.

    The reason is ``""`` when every entry of the window carries its complete
    per-finding digests. It is non-empty when an entry predates the
    ``resolutions`` field (a run persisted by an older controller) or was
    clipped by :data:`MAX_PERSISTED_RESOLUTION_DIGESTS`: such a window can
    still *prove* a recurrence, but it can never prove there was none, so the
    count-only behaviour applies. Malformed entries raise (see
    :func:`validate_review_history`) rather than being read as either.
    """
    validate_review_history(tail)
    seen: set[str] = set()
    recurring: set[str] = set()
    incomplete = ""
    for r in tail:
        if "resolutions" not in r:
            incomplete = incomplete or (
                f"round {r.get('round')} was written by an older controller and has no "
                "per-finding resolution digests, so recurrence cannot be ruled out"
            )
            continue
        if r.get("resolutions_truncated"):
            incomplete = (
                f"round {r.get('round')} persisted only the first "
                f"{MAX_PERSISTED_RESOLUTION_DIGESTS} resolution digest(s) of "
                f"{r.get('finding_count')} finding(s), so recurrence cannot be ruled out"
            )
        digests = set(r["resolutions"])
        recurring |= seen & digests
        seen |= digests
    return recurring, incomplete


def stagnation_reason(
    history: list[dict], identical_rounds: int, unchanged_count_rounds: int
) -> str:
    """Reason when the trailing review rounds show no progress; ``""`` otherwise.

    ``identical_rounds`` / ``unchanged_count_rounds`` of 0 disable the
    respective rule. Only *consecutive* rounds that ended with findings are
    considered: a clean or stale round in between resets both rules.

    The unchanged-count rule needs, on top of the constant count, at least
    one required resolution that was requested by two different rounds of
    the window: the count alone cannot tell an A/B/A ping-pong from a
    reviewer that raises one genuinely new finding per round (every earlier
    finding was resolved), and the latter is progress bounded by
    ``workflow.max_review_rounds``. A window whose recurrence evidence is
    incomplete — an entry written before the per-finding digests existed, or
    one clipped by :data:`MAX_PERSISTED_RESOLUTION_DIGESTS` — cannot rule a
    recurrence out and keeps the count-only behaviour; the reason says which.

    Both windows are 0 (disabled) or >= 2: a window of 1 compares a round
    with nothing and is rejected by the configuration loader.

    Raises :class:`StateError` when ``history`` holds a malformed entry.
    """
    validate_review_history(history)
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
        recurring, incomplete = _recurring_resolutions(tail)
        detail = (
            f"{len(recurring)} required resolution(s) recur across them"
            if recurring
            else incomplete
        )
        if detail:
            rounds = ", ".join(str(r.get("round")) for r in tail)
            return (
                f"review rounds {rounds} each ended with {tail[-1].get('finding_count')} "
                f"finding(s); the finding count has not changed and {detail} "
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
