"""Agent execution profile routing (pure, easily tested logic)."""

from __future__ import annotations

from .config import AutoForgeConfig, ProfileConfig
from .errors import ConfigurationError
from .transitions import Phase

REVIEW_ROUND_1 = "review_round_1"
REVIEW_ROUND_2_5 = "review_round_2_5"
REVIEW_ROUND_6_PLUS = "review_round_6_plus"


def review_profile_name(round: int) -> str:
    """Route a 1-based review round to its logical profile name.

    round == 1      -> review_round_1
    2 <= round <= 5 -> review_round_2_5
    round >= 6      -> review_round_6_plus
    """
    if round < 1:
        raise ConfigurationError(f"review round must be >= 1, got {round}")
    if round == 1:
        return REVIEW_ROUND_1
    if 2 <= round <= 5:
        return REVIEW_ROUND_2_5
    return REVIEW_ROUND_6_PLUS


def profile_for_phase(cfg: AutoForgeConfig, phase: Phase, review_round: int = 0) -> ProfileConfig:
    """Select the execution profile for a phase (REVIEW uses round routing)."""
    if phase == Phase.REVIEW:
        # review_round stores completed rounds; the upcoming execution is +1.
        upcoming = review_round + 1
        return cfg.profile(review_profile_name(upcoming))
    if phase == Phase.MERGE:
        raise ConfigurationError(
            "phase MERGE has no execution profile: the controller performs the merge "
            "itself (gh pr merge via GitHubClient); agents never merge"
        )
    mapping = {
        Phase.ANALYZE_EXECUTE: "analyze_execute",
        Phase.FIX: "fix",
        Phase.REPLAN_REEXECUTE: "replan_reexecute",
        Phase.UPDATE_EPIC: "update_epic",
    }
    if phase not in mapping:
        raise ConfigurationError(f"phase {phase.value} has no execution profile")
    return cfg.profile(mapping[phase])
