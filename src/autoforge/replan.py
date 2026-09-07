"""Deterministic REPLAN_REEXECUTE policy and compact history collection."""

from __future__ import annotations

from dataclasses import dataclass

from .config import ReplanConfig
from .github import GitHubClient
from .loop_guard import RESULT_NEEDS_FIX


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
) -> ReplanDecision:
    """Choose the post-review route without inspecting agent prose.

    A clean review always wins. ``max_replans_per_issue`` counts completed
    fresh reimplementations, not the original implementation attempt.
    """
    if not has_actionable_findings or not config.enabled:
        return ReplanDecision("continue_fix")

    trigger = ""
    metadata: dict[str, object] = {
        "current_round": current_review_round,
        "soft_threshold": config.soft_threshold,
        "hard_threshold": config.hard_threshold,
    }
    if current_review_round >= config.hard_threshold:
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
    recorded_finding_count: int = 0

    def render_findings(self) -> str:
        if not self.findings:
            return "(none)"
        return "\n".join(
            "- {id} (round {round}, {classification}): {required_resolution}".format(
                id=f.get("id", "(unknown)"),
                round=f.get("round", "?"),
                classification=f.get("classification", "unknown"),
                required_resolution=f.get("required_resolution", ""),
            )
            for f in self.findings
        )

    def render_observations(self) -> str:
        return "\n\n".join(self.observations) if self.observations else "(none)"

    def render_verification_failures(self) -> str:
        return "\n".join(self.verification_failures) if self.verification_failures else "(none)"


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
        recorded_finding_count = 0
        comments = {c.url: c for c in self.github.get_pr_comments(previous_pr_url)}
        remaining = self.max_comment_chars
        for record in review_history:
            if record.get("result") != RESULT_NEEDS_FIX:
                continue
            count = record.get("finding_count")
            if isinstance(count, int) and count >= 0:
                recorded_finding_count += count
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
            recorded_finding_count,
        )
