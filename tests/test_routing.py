"""Routing: review-round boundaries + per-phase profile selection."""

import pytest

from autoforge.config import default_config
from autoforge.errors import ConfigurationError
from autoforge.profiles import profile_for_phase, review_profile_name
from autoforge.transitions import Phase


@pytest.mark.parametrize(
    "round,expected",
    [
        (1, "review_round_1"),
        (2, "review_round_2_5"),
        (3, "review_round_2_5"),
        (5, "review_round_2_5"),
        (6, "review_round_6_plus"),
        (100, "review_round_6_plus"),
    ],
)
def test_review_round_boundaries(round, expected):
    assert review_profile_name(round) == expected


def test_review_round_zero_raises():
    with pytest.raises(ConfigurationError):
        review_profile_name(0)


def test_phase_profile_mapping():
    cfg = default_config()
    assert profile_for_phase(cfg, Phase.ANALYZE_EXECUTE).name == "analyze_execute"
    assert profile_for_phase(cfg, Phase.FIX).name == "fix"
    assert profile_for_phase(cfg, Phase.UPDATE_EPIC).name == "update_epic"
    # MERGE is executed by the controller (gh pr merge); no agent profile exists.
    with pytest.raises(ConfigurationError, match="controller performs the merge"):
        profile_for_phase(cfg, Phase.MERGE)
    # REVIEW uses completed-rounds + 1 as the upcoming round
    assert profile_for_phase(cfg, Phase.REVIEW, 0).name == "review_round_1"
    assert profile_for_phase(cfg, Phase.REVIEW, 1).name == "review_round_2_5"
    assert profile_for_phase(cfg, Phase.REVIEW, 4).name == "review_round_2_5"
    assert profile_for_phase(cfg, Phase.REVIEW, 5).name == "review_round_6_plus"
    with pytest.raises(ConfigurationError):
        profile_for_phase(cfg, Phase.DONE)
