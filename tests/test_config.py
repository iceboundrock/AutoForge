"""Config: defaults, example YAML loads, command building, validation URLs."""

import json

import pytest

from autoforge.config import default_config, load_config_file
from autoforge.errors import ConfigurationError
from autoforge.validation import parse_github_url, validate_epic_and_issue


def test_defaults_have_all_logical_profiles():
    cfg = default_config()
    for name in (
        "analyze_execute",
        "fix",
        "review_round_1",
        "review_round_2_5",
        "review_round_6_plus",
        "update_epic",
    ):
        p = cfg.profile(name)
        assert p.provider in ("claude", "opencode")
        assert p.model  # real CLI model identifier present
    # MERGE is performed by the controller: no agent profile, but a merge section.
    assert "merge" not in cfg.profiles
    assert cfg.merge.method == "squash" and cfg.merge.delete_branch is False
    assert cfg.merge.max_verification_attempts == 5


def test_merge_section_parsing(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(
        '{"version": 1, "merge": {"method": "rebase", "delete_branch": true}}', encoding="utf-8"
    )
    cfg = load_config_file(p)
    assert cfg.merge.method == "rebase" and cfg.merge.delete_branch is True
    p.write_text('{"version": 1, "merge": {"max_verification_attempts": 2}}', encoding="utf-8")
    assert load_config_file(p).merge.max_verification_attempts == 2
    for body in (
        '{"version": 1, "merge": {"method": "fast-forward"}}',
        '{"version": 1, "merge": {"method": 1}}',
        '{"version": 1, "merge": {"delete_branch": "true"}}',
        '{"version": 1, "merge": {"max_verification_attempts": 0}}',
        '{"version": 1, "merge": {"max_verification_attempts": "3"}}',
        '{"version": 1, "merge": {"max_verification_attempts": true}}',
        '{"version": 1, "merge": "squash"}',
    ):
        p.write_text(body, encoding="utf-8")
        with pytest.raises(ConfigurationError, match="merge"):
            load_config_file(p)


def test_unknown_profile_raises():
    with pytest.raises(ConfigurationError, match="unknown execution profile"):
        default_config().profile("nope")


def test_example_yaml_loads_without_pyyaml(tmp_path, monkeypatch):
    import sys
    from pathlib import Path

    import autoforge

    repo_example = Path(autoforge.__file__).parents[2] / "autoforge.example.yaml"
    assert repo_example.exists(), f"example config missing: {repo_example}"
    # Force the built-in subset parser even if PyYAML is installed.
    monkeypatch.setitem(sys.modules, "yaml", None)
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "yaml":
            raise ImportError("blocked for test")
        return real_import(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    cfg = load_config_file(repo_example)
    assert cfg.profile("analyze_execute").model == "fable"
    assert cfg.profile("review_round_1").model == "openai/gpt-5.6-luna"
    assert cfg.profile("review_round_2_5").model == "openai/gpt-5.6-terra"
    assert cfg.profile("review_round_6_plus").model == "openai/gpt-5.6-sol"
    assert cfg.profile("review_round_6_plus").effort == "medium"
    assert cfg.profile("analyze_execute").options["permission_mode"] == "bypassPermissions"
    assert cfg.safety.allow_merge is False
    assert cfg.merge_allowed_by_config is False
    assert cfg.execution.max_correction_attempts == 1


def test_json_config_overrides_model(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "profiles": {"update_epic": {"provider": "opencode", "model": "custom/m"}},
            }
        ),
        encoding="utf-8",
    )
    cfg = load_config_file(p)
    assert cfg.profile("update_epic").model == "custom/m"
    assert cfg.profile("fix").model == "fable"  # untouched default


def test_bad_version_rejected(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 999}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="version"):
        load_config_file(p)


def test_claude_command_shape():
    p = default_config().profile("analyze_execute")
    argv = p.build_command("hello prompt")
    assert argv[0] == "claude" and "-p" in argv and "--model" in argv
    assert "--permission-mode" in argv and "--effort" in argv
    assert argv[-1] == "hello prompt"  # single argv element, no shell


def test_safety_gate_from_either_location(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "safety": {"allow_merge": true}}', encoding="utf-8")
    assert load_config_file(p).merge_allowed_by_config is True
    p.write_text('{"version": 1, "execution": {"allow_merge": true}}', encoding="utf-8")
    assert load_config_file(p).merge_allowed_by_config is True
    p.write_text('{"version": 1}', encoding="utf-8")
    assert load_config_file(p).merge_allowed_by_config is False


def test_required_profiles_validation(tmp_path):
    from autoforge.config import validate_required_profiles
    from autoforge.engine import REQUIRED_PROFILES

    validate_required_profiles(default_config(), REQUIRED_PROFILES)
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "profiles": {"review_round_1": {"model": ""}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="review_round_1"):
        validate_required_profiles(load_config_file(p), REQUIRED_PROFILES)


def test_opencode_command_shape():
    p = default_config().profile("review_round_1")
    argv = p.build_command("review this")
    assert argv[:2] == ["opencode", "run"] and "-m" in argv and "--variant" in argv
    assert argv[-1] == "review this"


def test_url_validation():
    epic, issue = validate_epic_and_issue(
        "https://github.com/owner/repo/issues/1",
        "https://github.com/owner/repo/issues/2",
    )
    assert epic.repository == "owner/repo" and issue.number == 2
    with pytest.raises(ConfigurationError, match="same|repository|[Rr]epo"):
        validate_epic_and_issue(
            "https://github.com/owner/repo/issues/1",
            "https://github.com/other/repo/issues/2",
        )
    with pytest.raises(ConfigurationError):
        parse_github_url("http://github.com/o/r/issues/1")
    with pytest.raises(ConfigurationError):
        parse_github_url("https://github.com/o/r/issues/abc")
    with pytest.raises(ConfigurationError, match="expected a GitHub pr"):
        parse_github_url("https://github.com/o/r/issues/1", expect="pr")


# -- R1-F3: strict scalar coercion ------------------------------------------
@pytest.mark.parametrize(
    "body",
    [
        '{"version": 1, "safety": {"allow_merge": "false"}}',
        '{"version": 1, "safety": {"allow_merge": 1}}',
        '{"version": 1, "execution": {"allow_merge": "true"}}',
    ],
)
def test_allow_merge_rejects_non_boolean(tmp_path, body):
    """A quoted "false" must never open the merge gate via bool("false")."""
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="allow_merge.*must be a boolean"):
        load_config_file(p)


@pytest.mark.parametrize(
    "body, key",
    [
        (
            '{"version": 1, "execution": {"default_timeout_seconds": "not-a-number"}}',
            "execution.default_timeout_seconds",
        ),
        (
            '{"version": 1, "execution": {"max_correction_attempts": 1.5}}',
            "execution.max_correction_attempts",
        ),
        (
            '{"version": 1, "execution": {"default_timeout_seconds": true}}',
            "execution.default_timeout_seconds",
        ),
        ('{"version": 1, "github": {"timeout_seconds": "12"}}', "github.timeout_seconds"),
        (
            '{"version": 1, "profiles": {"fix": {"timeout_seconds": "60"}}}',
            "profiles.fix.timeout_seconds",
        ),
        (
            '{"version": 1, "profiles": {"custom": {"model": "m", "timeout_seconds": "60"}}}',
            "profiles.custom.timeout_seconds",
        ),
    ],
)
def test_integer_fields_reject_non_integers(tmp_path, body, key):
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=f"{key}.*must be an integer"):
        load_config_file(p)


def test_quoted_false_in_yaml_subset_is_rejected(tmp_path):
    """The built-in YAML subset parser keeps quoted scalars as strings."""
    p = tmp_path / "cfg.yaml"
    p.write_text('version: 1\nsafety:\n  allow_merge: "false"\n', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="safety.allow_merge"):
        load_config_file(p)


def test_real_scalars_still_accepted(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(
        '{"version": 1, "safety": {"allow_merge": true}, '
        '"execution": {"default_timeout_seconds": 42, "max_correction_attempts": 0}, '
        '"github": {"timeout_seconds": 7}, "profiles": {"fix": {"timeout_seconds": 9}}}',
        encoding="utf-8",
    )
    cfg = load_config_file(p)
    assert cfg.safety.allow_merge is True
    assert cfg.execution.default_timeout_seconds == 42
    assert cfg.execution.max_correction_attempts == 0
    assert cfg.github.timeout_seconds == 7
    assert cfg.profile("fix").timeout_seconds == 9


def test_workflow_defaults_and_parsing(tmp_path):
    cfg = default_config()
    assert cfg.workflow.max_review_rounds == 6
    assert cfg.workflow.stagnation_identical_rounds == 2
    assert cfg.workflow.stagnation_unchanged_count_rounds == 3
    assert cfg.workflow.max_total_steps == 300
    p = tmp_path / "cfg.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "workflow": {
                    "max_review_rounds": 3,
                    "stagnation_identical_rounds": 0,
                    "stagnation_unchanged_count_rounds": 0,
                    "max_total_steps": 12,
                },
            }
        ),
        encoding="utf-8",
    )
    wf = load_config_file(p).workflow
    assert (wf.max_review_rounds, wf.max_total_steps) == (3, 12)
    assert (wf.stagnation_identical_rounds, wf.stagnation_unchanged_count_rounds) == (0, 0)
    # 2 is the smallest window either rule can act on (see the rejection of 1).
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "workflow": {
                    "stagnation_identical_rounds": 2,
                    "stagnation_unchanged_count_rounds": 2,
                },
            }
        ),
        encoding="utf-8",
    )
    wf = load_config_file(p).workflow
    assert (wf.stagnation_identical_rounds, wf.stagnation_unchanged_count_rounds) == (2, 2)
    y = tmp_path / "cfg.yaml"
    y.write_text("version: 1\nworkflow:\n  max_review_rounds: 4\n", encoding="utf-8")
    assert load_config_file(y).workflow.max_review_rounds == 4


@pytest.mark.parametrize(
    "body,key",
    [
        ('{"version": 1, "workflow": {"max_review_rounds": 0}}', "max_review_rounds.*>= 1"),
        ('{"version": 1, "workflow": {"max_total_steps": 0}}', "max_total_steps.*>= 1"),
        (
            '{"version": 1, "workflow": {"stagnation_identical_rounds": -1}}',
            "stagnation_identical_rounds.*0 .rule disabled. or >= 2",
        ),
        (
            '{"version": 1, "workflow": {"stagnation_unchanged_count_rounds": "3"}}',
            "stagnation_unchanged_count_rounds.*must be an integer",
        ),
        # A window of 1 compares a round with nothing: it cannot hold a
        # recurrence, so it would silently disable the unchanged-count rule.
        (
            '{"version": 1, "workflow": {"stagnation_unchanged_count_rounds": 1}}',
            "stagnation_unchanged_count_rounds.*0 .rule disabled. or >= 2.*got 1",
        ),
        (
            '{"version": 1, "workflow": {"stagnation_identical_rounds": 1}}',
            "stagnation_identical_rounds.*0 .rule disabled. or >= 2.*got 1",
        ),
        ('{"version": 1, "workflow": {"max_review_rounds": true}}', "must be an integer"),
        ('{"version": 1, "workflow": 6}', "'workflow' must be a mapping"),
    ],
)
def test_workflow_section_rejects_invalid_values(tmp_path, body, key):
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=key):
        load_config_file(p)
