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


# The only profile every LOCAL run reaches whatever its bound is. `fix` and
# the reviewer profiles are conditional (see `local_required_profiles`).
LOCAL_BASE_PROFILES = ["analyze_execute"]

# Lowest review round that routes to each reviewer profile, matching
# `review_profile_name`. Deriving the requirement from these thresholds keeps
# `local_required_profiles` O(1): a configured bound of 10**9 fix rounds still
# reaches exactly three reviewer profiles, so validation must not walk the
# rounds one by one to discover that.
REVIEW_PROFILE_THRESHOLDS: tuple[tuple[int, str], ...] = (
    (1, REVIEW_ROUND_1),
    (2, REVIEW_ROUND_2_5),
    (6, REVIEW_ROUND_6_PLUS),
)


def local_required_profiles(cfg: AutoForgeConfig) -> list[str]:
    """Profiles a LOCAL run can actually reach, given its configured bound.

    A local run never replans and never updates an EPIC, so those profiles are
    not required. The rest follows from ``local.max_fix_rounds``:

    - ``fix`` only when at least one fix round is allowed. With
      ``max_fix_rounds == 0`` a review with findings goes straight to BLOCKED
      and FIX is never invoked, so requiring a valid ``fix`` profile would
      reject a perfectly runnable one-pass configuration.
    - the reviewer profiles follow exactly the same round routing as remote:
      with ``local.max_fix_rounds >= 5`` a local run can complete review round
      6 and will ask for ``review_round_6_plus``. Deriving the requirement
      from the configured bound keeps a missing reviewer profile a config-time
      failure instead of one discovered five fix rounds into a run; the
      inverse also holds, so ``max_fix_rounds == 0`` requires only
      ``review_round_1``.
    """
    names = list(LOCAL_BASE_PROFILES)
    if cfg.local.max_fix_rounds > 0:
        names.append("fix")
    rounds = cfg.local.max_review_rounds
    names.extend(name for threshold, name in REVIEW_PROFILE_THRESHOLDS if rounds >= threshold)
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
