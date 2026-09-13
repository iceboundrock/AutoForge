"""Config: defaults, example YAML loads, command building, validation URLs."""

import json

import pytest

from autoforge.config import default_config, load_config_file, validate_required_profiles
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
        "replan_reexecute",
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


# -- AF-SEC-001: exactly one key opens the merge gate ------------------------
def test_safety_gate_opens_from_safety_allow_merge_only(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "safety": {"allow_merge": true}}', encoding="utf-8")
    cfg = load_config_file(p)
    assert cfg.merge_allowed_by_config is True
    assert cfg.safety.allow_merge_source == f"{p}: safety.allow_merge"
    p.write_text('{"version": 1, "safety": {"allow_merge": false}}', encoding="utf-8")
    cfg = load_config_file(p)
    assert cfg.merge_allowed_by_config is False
    assert cfg.safety.allow_merge_source == f"{p}: safety.allow_merge"
    p.write_text('{"version": 1}', encoding="utf-8")
    cfg = load_config_file(p)
    assert cfg.merge_allowed_by_config is False
    assert cfg.safety.allow_merge_source == "built-in default"
    assert default_config().safety.allow_merge_source == "built-in default"


@pytest.mark.parametrize(
    "body",
    [
        # The issue's failure scenario: a config copied from an old example
        # opens the gate under `execution`, the operator later "closes" it
        # under `safety`, and --allow-merge would merge.
        '{"version": 1, "execution": {"allow_merge": true}, "safety": {"allow_merge": false}}',
        '{"version": 1, "execution": {"allow_merge": true}}',
        # Not even `false` is read: the key is gone, not tolerated.
        '{"version": 1, "execution": {"allow_merge": false}}',
        # Rejected for being present, before its value is even type-checked.
        '{"version": 1, "execution": {"allow_merge": "true"}}',
    ],
)
def test_deprecated_execution_allow_merge_is_a_hard_error(tmp_path, body):
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="execution.allow_merge.*no longer supported"):
        load_config_file(p)


def test_execution_config_has_no_allow_merge_field():
    """The deprecated key cannot survive as an attribute nothing reads or validates."""
    from dataclasses import fields

    from autoforge.config import ExecutionConfig

    assert "allow_merge" not in {f.name for f in fields(ExecutionConfig)}


@pytest.mark.parametrize(
    "body",
    [
        '{"version": 1, "safety": {"allow_merges": true}}',
        '{"version": 1, "safety": {"allow_merge": true, "allowMerge": false}}',
        '{"version": 1, "safety": {"protected_paths": []}}',
    ],
)
def test_unknown_safety_keys_are_rejected_not_ignored(tmp_path, body):
    """A misspelled gate key fails loudly rather than silently leaving the gate closed."""
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="unknown key.*under 'safety'") as info:
        load_config_file(p)
    message = str(info.value)
    unknown = [k for k in ("allow_merges", "allowMerge", "protected_paths") if k in body]
    assert all(k in message for k in unknown)
    assert "allow_merge, protected_merge_paths, required_checks, verify_check_definition" in (
        message
    )  # known keys named


def test_unknown_safety_key_rejected_in_yaml_too(tmp_path):
    p = tmp_path / "cfg.yaml"
    p.write_text("version: 1\nsafety:\n  allow_merges: true\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="allow_merges"):
        load_config_file(p)


# Every JSON falsy value that is not a mapping. `data.get("safety", {}) or {}`
# used to turn all of these into an omitted section, so the "must be a
# mapping" check right after it was unreachable for exactly the values a
# hand edit or a generator is most likely to produce.
FALSY_NON_MAPPINGS = ["[]", "false", '""', "0"]


@pytest.mark.parametrize("value", FALSY_NON_MAPPINGS + ['"yes"', "[1]", "1"])
def test_non_mapping_safety_section_is_rejected(tmp_path, value):
    """A present `safety` that is not a mapping fails loudly, whatever its truthiness."""
    p = tmp_path / "cfg.json"
    p.write_text(f'{{"version": 1, "safety": {value}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="'safety' must be a mapping"):
        load_config_file(p)


@pytest.mark.parametrize("value", FALSY_NON_MAPPINGS)
def test_non_mapping_safety_section_rejected_in_yaml_too(tmp_path, value):
    p = tmp_path / "cfg.yaml"
    p.write_text(f"version: 1\nsafety: {value}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="'safety' must be a mapping"):
        load_config_file(p)


@pytest.mark.parametrize(
    "section",
    ["execution", "github", "merge", "review", "workflow", "local", "profiles"],
)
@pytest.mark.parametrize("value", FALSY_NON_MAPPINGS)
def test_non_mapping_sections_are_rejected_everywhere(tmp_path, section, value):
    """The same contract for every section: the fix is in the reader, not in `safety`."""
    p = tmp_path / "cfg.json"
    p.write_text(f'{{"version": 1, "{section}": {value}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match=f"'{section}' must be a mapping"):
        load_config_file(p)


@pytest.mark.parametrize("value", FALSY_NON_MAPPINGS)
def test_non_mapping_replan_section_is_rejected(tmp_path, value):
    p = tmp_path / "cfg.json"
    p.write_text(f'{{"version": 1, "review": {{"replan": {value}}}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="'review.replan' must be a mapping"):
        load_config_file(p)


def test_empty_safety_section_header_is_the_default(tmp_path):
    """`safety:` with every child commented out is how a hand-edited YAML file
    looks; it reads as null and yields the same fail-closed defaults as omitting
    the section. An explicit JSON null is the same value and the same case."""
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text("version: 1\nsafety:\n  # allow_merge: true\nmerge:\n", encoding="utf-8")
    cfg = load_config_file(yaml)
    assert cfg.merge_allowed_by_config is False
    assert cfg.safety.allow_merge_source == "built-in default"
    assert cfg.safety.protected_merge_paths == default_config().safety.protected_merge_paths
    js = tmp_path / "cfg.json"
    js.write_text('{"version": 1, "safety": null}', encoding="utf-8")
    assert load_config_file(js).safety == default_config().safety


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
    assert cfg.workflow.max_review_rounds == 20
    assert cfg.workflow.stagnation_identical_rounds == 2
    assert cfg.workflow.stagnation_unchanged_count_rounds == 3
    assert cfg.workflow.max_total_steps == 300
    assert cfg.review.replan.soft_threshold == 12
    assert cfg.review.replan.hard_threshold == 20
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
                "review": {"replan": {"soft_threshold": 2, "hard_threshold": 3}},
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
    y.write_text(
        "version: 1\nworkflow:\n  max_review_rounds: 4\nreview:\n  replan:\n"
        "    soft_threshold: 4\n    hard_threshold: 4\n",
        encoding="utf-8",
    )
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


@pytest.mark.parametrize(
    "body,needle",
    [
        ('{"version": 1, "review": {"replan": {"soft_threshold": 0}}}', "soft_threshold.*>= 1"),
        (
            '{"version": 1, "review": {"replan": {"hard_threshold": 11}}}',
            "hard_threshold.*soft_threshold",
        ),
        (
            '{"version": 1, "review": {"replan": {"stagnation_window": 0}}}',
            "stagnation_window.*>= 1",
        ),
        ('{"version": 1, "review": {"replan": {"max_findings_per_round": -1}}}', "max_findings"),
        ('{"version": 1, "review": {"replan": {"max_replans_per_issue": -1}}}', "max_replans"),
        ('{"version": 1, "review": {"replan": {"enabled": "false"}}}', "enabled.*boolean"),
        (
            '{"version": 1, "workflow": {"max_review_rounds": 19}}',
            "hard_threshold.*max_review_rounds",
        ),
    ],
)
def test_replan_config_validation(tmp_path, body, needle):
    path = tmp_path / "cfg.json"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=needle):
        load_config_file(path)


# -- safety.protected_merge_paths (PR #38 review R2-F1) -------------------------------
def test_default_protected_merge_paths_cover_the_workflow_definitions():
    """The hosted checks the merge gate trusts are defined by these files."""
    safety = default_config().safety
    assert safety.protects(".github/workflows/ci.yml")
    assert safety.protects(".github/workflows/nested/other.yaml")
    assert safety.protects(".github/workflows")  # the directory itself (rename/delete)


@pytest.mark.parametrize(
    "path",
    [
        "src/autoforge/engine.py",
        ".github/ISSUE_TEMPLATE.md",
        ".github/workflows-notes.md",  # prefix must not match a sibling name
        "docs/.github/workflows/ci.yml",  # only repo-root paths are protected
    ],
)
def test_unprotected_paths_are_not_matched(path):
    assert not default_config().safety.protects(path)


def test_protected_merge_paths_accept_exact_paths_and_patterns(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "safety": {"protected_merge_paths": ["Makefile", "*.lock", "ci/"]},
            }
        ),
        encoding="utf-8",
    )
    safety = load_config_file(p).safety
    assert safety.protected_merge_paths == ["Makefile", "*.lock", "ci/"]
    assert safety.protects("Makefile") and safety.protects("uv.lock")
    assert safety.protects("ci/run.sh") and not safety.protects("cirrus/run.sh")
    assert not safety.protects("src/Makefile.in")


def test_empty_protected_merge_paths_disables_the_gate(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"version": 1, "safety": {"protected_merge_paths": []}}), "utf-8")
    safety = load_config_file(p).safety
    assert safety.protected_merge_paths == []
    assert not safety.protects(".github/workflows/ci.yml")


def test_null_protected_merge_paths_is_a_configuration_error(tmp_path):
    """`[]` is the deliberate opt-out; a value that went missing must not become one."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"version": 1, "safety": {"protected_merge_paths": None}}), "utf-8")
    with pytest.raises(ConfigurationError, match="protected_merge_paths is null"):
        load_config_file(p)


def test_a_yaml_key_left_empty_does_not_disable_the_gate(tmp_path):
    """The shape a hand-edited config actually takes: the key with no value."""
    p = tmp_path / "cfg.yaml"
    p.write_text("version: 1\nsafety:\n  protected_merge_paths:\n  allow_merge: true\n", "utf-8")
    with pytest.raises(ConfigurationError, match="protected_merge_paths is null"):
        load_config_file(p)


@pytest.mark.parametrize(
    "value",
    [".github/workflows/", 5, [".github/workflows/", 7], ["  "]],
)
def test_protected_merge_paths_must_be_a_list_of_non_empty_strings(tmp_path, value):
    """A bare string is rejected rather than silently iterated per character."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"version": 1, "safety": {"protected_merge_paths": value}}), "utf-8")
    with pytest.raises(ConfigurationError, match="protected_merge_paths"):
        load_config_file(p)


@pytest.mark.parametrize("value", [0, -1, -1800])
def test_a_non_positive_default_timeout_is_rejected(tmp_path, value):
    """PR #44, R1-F6: `timeout=0` disables the timeout rather than tightening it.

    `subprocess.run(..., timeout=0)` is not "fail immediately", and a negative
    value is not a timeout at all — both leave a hung agent running forever
    against the operator's machine, which is exactly what the timeout exists
    to bound. A configuration that reads as "no time allowed" must not
    silently become "unlimited time".
    """
    p = tmp_path / "cfg.json"
    p.write_text(
        f'{{"version": 1, "execution": {{"default_timeout_seconds": {value}}}}}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="default_timeout_seconds.*must be > 0"):
        load_config_file(p)


@pytest.mark.parametrize("value", [0, -5])
def test_a_non_positive_profile_timeout_is_rejected(tmp_path, value):
    """The same bound on the per-profile override that shadows the default.

    The profile override is checked when the profile is validated (the
    controller's preflight and `doctor`) rather than at parse time, because a
    config may legitimately carry profiles a given run never reaches.
    """
    p = tmp_path / "cfg.json"
    p.write_text(
        f'{{"version": 1, "profiles": {{"fix": {{"timeout_seconds": {value}}}}}}}',
        encoding="utf-8",
    )
    cfg = load_config_file(p)
    assert cfg.profile("fix").timeout_seconds == value
    with pytest.raises(ConfigurationError, match="timeout_seconds must be > 0"):
        validate_required_profiles(cfg, ["fix"])


def test_a_positive_default_timeout_is_still_accepted(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "execution": {"default_timeout_seconds": 1}}', encoding="utf-8")
    assert load_config_file(p).execution.default_timeout_seconds == 1


# -- the `local:` block ---------------------------------------------------------
def _local_cfg(tmp_path, local: dict):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"version": 1, "local": local}), encoding="utf-8")
    return load_config_file(p)


def test_local_defaults_are_the_documented_ones():
    local = default_config().local
    assert local.feature_dir == "features"
    assert local.max_fix_rounds == 1
    assert local.validation_commands == []
    assert local.exclude == []
    assert local.max_workspace_entries == 50_000
    assert local.max_workspace_bytes == 512 * 1024 * 1024


def test_local_exclude_is_normalized_deduplicated_and_sorted(tmp_path):
    """The list is hashed into the fingerprint and frozen with the run, so two
    spellings of one policy have to reduce to one text."""
    cfg = _local_cfg(tmp_path, {"exclude": ["build/", ".venv", "build", "/target/"]})
    assert cfg.local.exclude == [".venv", "build", "target"]


@pytest.mark.parametrize(
    ("local", "match"),
    [
        ({"exclude": ".venv"}, "must be a list"),
        ({"exclude": [".venv", 7]}, "must be a string"),
        ({"exclude": [""]}, "must not be empty"),
        ({"exclude": ["../outside"]}, "may not contain"),
        ({"exclude": ["a/./b"]}, "may not contain"),
        ({"feature_dir": "   "}, "must not be empty"),
        ({"max_fix_rounds": -1}, "must be >= 0"),
        ({"max_workspace_entries": 0}, "must be >= 1"),
        ({"max_workspace_bytes": 0}, "must be >= 1"),
        ({"validation_commands": ["uv run pytest"]}, "validation_commands"),
        ({"validation_commands": [[]]}, "validation_commands"),
    ],
)
def test_local_block_rejects_unusable_values(tmp_path, local, match):
    with pytest.raises(ConfigurationError, match=match):
        _local_cfg(tmp_path, local)


def test_local_validation_commands_stay_argv_arrays(tmp_path):
    cfg = _local_cfg(
        tmp_path, {"validation_commands": [["uv", "run", "pytest", "-q"], ["./gradlew", "test"]]}
    )
    assert cfg.local.validation_commands == [
        ["uv", "run", "pytest", "-q"],
        ["./gradlew", "test"],
    ]


def test_a_non_mapping_local_block_is_refused(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "local": ["features"]}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="'local' must be a mapping"):
        load_config_file(p)


# -- safety.required_checks (issue #41) -------------------------------------------------
def test_required_checks_default_and_override(tmp_path):
    """`doctor` verifies the default branch requires these contexts; default is `ci`."""
    assert default_config().safety.required_checks == ["ci"]
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "safety": {"required_checks": ["build", " lint "]}}')
    assert load_config_file(p).safety.required_checks == ["build", "lint"]
    p.write_text('{"version": 1, "safety": {"required_checks": []}}')
    assert load_config_file(p).safety.required_checks == []
    p.write_text('{"version": 1, "safety": {"required_checks": null}}')
    with pytest.raises(ConfigurationError, match="required_checks is null"):
        load_config_file(p)
    p.write_text('{"version": 1, "safety": {"required_checks": "ci"}}')
    with pytest.raises(ConfigurationError, match="required_checks must be a list"):
        load_config_file(p)


# -- #42: the controller's own pre-merge evidence ---------------------------------------
def test_premerge_verification_defaults():
    cfg = default_config()
    assert cfg.safety.verify_check_definition is True
    assert cfg.merge.verification_commands == []


def test_premerge_verification_keys_load(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(
        json.dumps(
            {
                "version": 1,
                "safety": {"verify_check_definition": False},
                "merge": {"verification_commands": [["uv", "run", "pytest", "-q"], ["make"]]},
            }
        ),
        "utf-8",
    )
    cfg = load_config_file(p)
    assert cfg.safety.verify_check_definition is False
    assert cfg.merge.verification_commands == [["uv", "run", "pytest", "-q"], ["make"]]


def test_verification_command_argv_elements_are_stored_verbatim(tmp_path):
    """Surrounding whitespace in an argv element is the value, never trimmed away."""
    p = tmp_path / "cfg.json"
    argv = ["pytest", " -k", "smoke "]
    p.write_text(json.dumps({"version": 1, "merge": {"verification_commands": [argv]}}), "utf-8")
    assert load_config_file(p).merge.verification_commands == [argv]


@pytest.mark.parametrize(
    ("section", "body", "key"),
    [
        ("safety", {"verify_check_definition": None}, "verify_check_definition"),
        ("safety", {"verify_check_definition": "yes"}, "verify_check_definition"),
        ("merge", {"verification_commands": None}, "verification_commands"),
        ("merge", {"verification_commands": "make check"}, "verification_commands"),
        ("merge", {"verification_commands": ["make check"]}, "verification_commands"),
        ("merge", {"verification_commands": [[]]}, "verification_commands"),
        ("merge", {"verification_commands": [["make", 1]]}, "verification_commands"),
        ("merge", {"verification_commands": [["make", "  "]]}, "verification_commands"),
    ],
)
def test_premerge_verification_keys_reject_bad_shapes(tmp_path, section, body, key):
    """A shell string or a missing value never silently becomes "run nothing"."""
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"version": 1, section: body}), "utf-8")
    with pytest.raises(ConfigurationError, match=key):
        load_config_file(p)
