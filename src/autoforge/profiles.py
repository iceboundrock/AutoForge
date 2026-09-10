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


# Profiles every LOCAL run needs whatever its review bound is. The reviewer
# profiles are not listed here: which of them a local run can reach depends on
# `local.max_fix_rounds` (see `local_required_profiles`).
LOCAL_BASE_PROFILES = ["analyze_execute", "fix"]


def local_required_profiles(cfg: AutoForgeConfig) -> list[str]:
    """Profiles a LOCAL run can actually reach, given its configured bound.

    A local run never replans and never updates an EPIC, so those profiles are
    not required. Its reviewer profiles, however, follow exactly the same round
    routing as remote: with ``local.max_fix_rounds >= 5`` a local run can
    complete review round 6 and will ask for ``review_round_6_plus``. Deriving
    the requirement from the configured bound keeps a missing reviewer profile
    a config-time failure instead of one discovered five fix rounds into a run.

    The inverse also holds: with ``max_fix_rounds == 0`` only round 1 is
    reachable, so ``review_round_2_5`` is not required.
    """
    names = list(LOCAL_BASE_PROFILES)
    for round in range(1, cfg.local.max_review_rounds + 1):
        name = review_profile_name(round)
        if name not in names:
            names.append(name)
    return names


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
