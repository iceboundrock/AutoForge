"""Routing: review-round boundaries + per-phase profile selection."""

import pytest

from autoforge.config import default_config
from autoforge.errors import ConfigurationError
from autoforge.profiles import REQUIRED_PROFILES, profile_for_phase, review_profile_name
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
    assert profile_for_phase(cfg, Phase.REPLAN_REEXECUTE).name == "replan_reexecute"
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


def test_required_profiles_cover_every_phase_a_remote_run_can_reach():
    """#7 (item 3): the one REQUIRED_PROFILES list must agree with the routing
    it guards, so a profile a REMOTE run can select is always required
    (`update_epic` excepted: that phase is still gated)."""
    cfg = default_config()
    reachable = {
        profile_for_phase(cfg, phase).name
        for phase in (Phase.ANALYZE_EXECUTE, Phase.FIX, Phase.REPLAN_REEXECUTE)
    }
    reachable |= {review_profile_name(r) for r in (1, 2, 5, 6, 50)}
    assert reachable == set(REQUIRED_PROFILES)
    assert len(REQUIRED_PROFILES) == len(set(REQUIRED_PROFILES))


@pytest.mark.parametrize(
    "phase,completed_rounds,expected",
    [
        (Phase.ANALYZE_EXECUTE, 0, "analyze_execute"),
        (Phase.REVIEW, 0, "review_round_1"),
        (Phase.FIX, 1, "fix"),
        (Phase.REVIEW, 1, "review_round_2_5"),
        (Phase.REVIEW, 4, "review_round_2_5"),
        (Phase.REVIEW, 5, "review_round_6_plus"),
        (Phase.REVIEW, 49, "review_round_6_plus"),
        (Phase.REPLAN_REEXECUTE, 12, "replan_reexecute"),
        (Phase.UPDATE_EPIC, 0, "update_epic"),
    ],
)
def test_a_pi_profile_routes_its_own_model_and_thinking_level(
    tmp_path, phase, completed_rounds, expected
):
    """#133: with every profile on ``provider: pi`` the routing is unchanged,
    the config validates for REMOTE and LOCAL, and the argv the adapter
    renders for the routed profile carries that profile's ``--model`` and
    ``--thinking`` and never the prompt (Pi receives it over RPC)."""
    from autoforge.config import validate_required_profiles
    from autoforge.profiles import local_required_profiles
    from autoforge.providers import PiProvider, provider_for
    from tests.pi_fake import PI_PROFILES, PiFake, flag, route_to_pi

    cfg = default_config()
    route_to_pi(cfg, PiFake(tmp_path / "pi"))
    validate_required_profiles(cfg, REQUIRED_PROFILES)
    validate_required_profiles(cfg, local_required_profiles(cfg))

    profile = profile_for_phase(cfg, phase, completed_rounds)
    assert profile.name == expected and profile.provider == "pi"
    assert isinstance(provider_for(profile), PiProvider)
    argv = profile.build_command("PROMPT-SENTINEL")
    assert (flag(argv, "--model"), flag(argv, "--thinking")) == PI_PROFILES[expected]
    assert not any("PROMPT-SENTINEL" in arg for arg in argv)
