"""Deterministic REPLAN_REEXECUTE policy and compact history collection."""

from __future__ import annotations

import re
from dataclasses import dataclass

from .config import ReplanConfig
from .github import GitHubClient
from .loop_guard import RESULT_NEEDS_FIX

# Any run of three or more tildes can close (or open) a tilde code fence, so
# the escape must break *runs*, not the literal fence string: replacing only
# "~~~~" is non-overlapping and left-to-right, which lets "~~~~~~~" re-form a
# closing fence out of the untouched tail.
_TILDE_RUN_RE = re.compile(r"~{3,}")


@dataclass(frozen=True)
class ReplanDecision:
    action: str  # continue_fix | replan | block_for_human
    reason: str = ""
    metadata: dict[str, object] | None = None


def evaluate_replan_policy(
    *,
    has_actionable_findings: bool,
    current_review_round: int,
    review_history: list[dict],
    escalation_count: int,
    config: ReplanConfig,
    workflow_stagnation_reason: str = "",
) -> ReplanDecision:
    """Choose the post-review route without inspecting agent prose.

    A clean review always wins. ``max_replans_per_issue`` counts completed
    fresh reimplementations, not the original implementation attempt.

    ``soft_threshold`` is the review round from which the controller may
    *replace* an implementation instead of continuing to patch it. Every
    stagnation trigger is gated behind it, including the ``workflow.stagnation_*``
    verdict passed in as ``workflow_stagnation_reason``: an early streak of
    identical resolutions is usually a single FIX round that missed a finding,
    and discarding the whole PR over that is far more destructive than the
    documented BLOCKED. Below the threshold this returns ``continue_fix`` and
    the caller's ordinary loop bounds (cap / stagnation -> BLOCKED) apply
    unchanged; at or above it, sustained non-convergence escalates to a replan.
    """
    if not has_actionable_findings or not config.enabled:
        return ReplanDecision("continue_fix")

    trigger = ""
    metadata: dict[str, object] = {
        "current_round": current_review_round,
        "soft_threshold": config.soft_threshold,
        "hard_threshold": config.hard_threshold,
    }
    if workflow_stagnation_reason and current_review_round >= config.soft_threshold:
        trigger = "workflow_stagnation"
        metadata["workflow_stagnation_reason"] = workflow_stagnation_reason
    elif current_review_round >= config.hard_threshold:
        trigger = "hard_review_round_threshold"
    elif current_review_round >= config.soft_threshold:
        window = config.stagnation_window
        tail = review_history[-window:]
        counts = [r.get("finding_count") for r in tail]
        metadata.update(
            {
                "stagnation_window": window,
                "max_findings_per_round": config.max_findings_per_round,
                "recent_finding_counts": counts,
            }
        )
        if (
            len(tail) == window
            and all(r.get("result") == RESULT_NEEDS_FIX for r in tail)
            and all(
                isinstance(count, int) and count <= config.max_findings_per_round
                for count in counts
            )
        ):
            trigger = "stagnation_after_soft_threshold"
    if not trigger:
        return ReplanDecision("continue_fix")
    metadata["trigger"] = trigger
    metadata["replan_count"] = escalation_count
    metadata["max_replans_per_issue"] = config.max_replans_per_issue
    if escalation_count >= config.max_replans_per_issue:
        return ReplanDecision("block_for_human", "replan_limit_exceeded", metadata)
    return ReplanDecision("replan", trigger, metadata)


@dataclass(frozen=True)
class HistoricalReviewData:
    findings: list[dict]
    observations: list[str]
    verification_failures: list[str]
    # Number of findings actually rendered into the prompt. The controller
    # requires the agent to account for exactly these; counting rounds'
    # untruncated ``finding_count`` instead would demand it account for
    # findings it was never shown (loop_guard truncates at
    # MAX_PERSISTED_FINDINGS_PER_ROUND).
    recorded_finding_count: int = 0

    @staticmethod
    def _render_untrusted(text: str) -> str:
        """Prevent collected text from terminating the prompt's outer fence.

        Every run of 3+ tildes is broken apart, so no residual run of any
        length (indented or not) can act as a fence delimiter.
        """
        return _TILDE_RUN_RE.sub(lambda m: " ".join(m.group(0)), text)

    def render_findings(self) -> str:
        if not self.findings:
            return "(none)"
        return self._render_untrusted("\n".join(
            "- {id} (round {round}, {classification}): {required_resolution}".format(
                id=f.get("id", "(unknown)"),
                round=f.get("round", "?"),
                classification=f.get("classification", "unknown"),
                required_resolution=f.get("required_resolution", ""),
            )
            for f in self.findings
        ))

    def render_observations(self) -> str:
        if not self.observations:
            return "(none)"
        return self._render_untrusted("\n\n".join(self.observations))

    def render_verification_failures(self) -> str:
        if not self.verification_failures:
            return "(none)"
        return self._render_untrusted("\n".join(self.verification_failures))


class HistoricalReviewCollector:
    """Collect bounded, typed review evidence before an implementation is abandoned.

    State carries compact finding summaries and GitHub remains the source for
    review-comment bodies. Only comments referenced by the persisted review
    rounds are collected; arbitrary PR conversation is never injected.
    """

    def __init__(self, github: GitHubClient, max_comment_chars: int = 24000) -> None:
        self.github = github
        self.max_comment_chars = max_comment_chars

    def collect(
        self,
        previous_pr_url: str,
        review_history: list[dict],
        verification_failures: list[str],
    ) -> HistoricalReviewData:
        findings: list[dict] = []
        observations: list[str] = []
        comments = {c.url: c for c in self.github.get_pr_comments(previous_pr_url)}
        remaining = self.max_comment_chars
        for record in review_history:
            if record.get("result") != RESULT_NEEDS_FIX:
                continue
            round_number = record.get("round")
            for finding in record.get("findings", []):
                if isinstance(finding, dict):
                    findings.append({"round": round_number, **finding})
            comment_url = record.get("review_comment_url", "")
            comment = comments.get(comment_url)
            if comment is not None and comment.body and remaining > 0:
                body = comment.body[:remaining]
                observations.append(
                    f"Review round {round_number} comment ({comment.url}, untrusted data):\n{body}"
                )
                remaining -= len(body)
        return HistoricalReviewData(
            findings,
            observations,
            list(verification_failures),
            len(findings),
        )
