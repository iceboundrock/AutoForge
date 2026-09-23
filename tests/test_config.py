"""Config: defaults, example YAML loads, command building, validation URLs."""

import builtins
import json
import sys
import tomllib
from contextlib import contextmanager
from pathlib import Path

import pytest

from autoforge import config
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


@contextmanager
def pyyaml_hidden(monkeypatch):
    """``import yaml`` raises inside the block, as it does without the extra."""
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "yaml", None)
        real_import = builtins.__import__

        def fake_import(name, *a, **k):
            if name == "yaml":
                raise ImportError("blocked for test")
            return real_import(name, *a, **k)

        m.setattr(builtins, "__import__", fake_import)
        yield


@pytest.fixture
def no_pyyaml(monkeypatch):
    """Force the built-in YAML subset parser even though PyYAML is installed."""
    with pyyaml_hidden(monkeypatch):
        yield


@pytest.fixture(params=["pyyaml", "subset"])
def yaml_backend(request):
    """Run a YAML case under PyYAML and under the subset parser.

    An operator with ``autoforge[yaml]`` reads every config through PyYAML,
    and PyYAML's own answers (last duplicate wins, any root type, typed keys)
    are exactly where the loader's fail-closed rules need their own check,
    so a rule proven only on the subset parser is proven on the wrong path.
    """
    if request.param == "pyyaml":
        pytest.importorskip("yaml")
    else:
        request.getfixturevalue("no_pyyaml")
    return request.param


def test_example_yaml_loads_without_pyyaml(tmp_path, no_pyyaml):
    import autoforge

    repo_example = Path(autoforge.__file__).parents[2] / "autoforge.example.yaml"
    assert repo_example.exists(), f"example config missing: {repo_example}"
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


# One (section label, path into the JSON document, a valid key with a value,
# the known keys as `_merge_config` names them) per section that has a closed
# key set. `profiles` is keyed by name, so its entry is one profile mapping.
SECTION_SCHEMAS = [
    ("the top level", (), ("state_dir", '".autoforge"'), config.TOP_LEVEL_KEYS),
    ("execution", ("execution",), ("max_correction_attempts", "1"), config.EXECUTION_KEYS),
    ("safety", ("safety",), ("allow_merge", "false"), config.SAFETY_KEYS),
    ("github", ("github",), ("timeout_seconds", "60"), config.GITHUB_KEYS),
    ("merge", ("merge",), ("method", '"squash"'), config.MERGE_KEYS),
    ("review", ("review",), ("replan", "{}"), config.REVIEW_KEYS),
    ("review.replan", ("review", "replan"), ("enabled", "false"), config.REPLAN_KEYS),
    ("workflow", ("workflow",), ("max_review_rounds", "3"), config.WORKFLOW_KEYS),
    ("local", ("local",), ("max_fix_rounds", "0"), config.LOCAL_KEYS),
    ("profiles.fix", ("profiles", "fix"), ("effort", '"high"'), config.PROFILE_KEYS),
]


def _document(path, entries):
    """A JSON config with ``entries`` (``"key": value`` strings) at ``path``."""
    body = "{" + ", ".join(entries) + "}"
    for key in reversed(path):
        body = f'{{"{key}": {body}}}'
    if not path:
        body = "{" + ", ".join(['"version": 1'] + entries) + "}"
    return body


@pytest.mark.parametrize("label, path, valid, known", SECTION_SCHEMAS, ids=lambda v: str(v)[:20])
@pytest.mark.parametrize("beside_valid_key", [False, True])
def test_unknown_keys_are_rejected_in_every_section(
    tmp_path, label, path, valid, known, beside_valid_key
):
    """A misspelled key is an error everywhere, not a no-op that keeps the default.

    The message names the section, the offending key(s) and the keys the
    loader would have read, in the shape `safety` already used.
    """
    entries = [f'"{valid[0]}": {valid[1]}'] if beside_valid_key else []
    entries += ['"bogus_key": 1', '"another_bogus": true']
    p = tmp_path / "cfg.json"
    p.write_text(_document(path, entries), encoding="utf-8")
    with pytest.raises(ConfigurationError, match=f"unknown key.*under '{label}'") as info:
        load_config_file(p)
    message = str(info.value)
    assert "another_bogus, bogus_key" in message  # every unknown key, sorted
    assert f"(known: {', '.join(known)})" in message
    assert valid[0] not in message.split("(known:")[0]  # the valid key is not blamed


@pytest.mark.parametrize(
    "body, label, key",
    [
        # The examples from issue #59, each of which used to load without error.
        ('{"version": 1, "merge": {"methd": "squash"}}', "merge", "methd"),
        ('{"version": 1, "workflow": {"max_review_round": 3}}', "workflow", "max_review_round"),
        (
            '{"version": 1, "workflow": {"stagnation_identical_round": 2}}',
            "workflow",
            "stagnation_identical_round",
        ),
        ('{"version": 1, "review": {"replan": {"enable": false}}}', "review.replan", "enable"),
        (
            '{"version": 1, "execution": {"default_timeout_second": 60}}',
            "execution",
            "default_timeout_second",
        ),
        # A key that exists, but under another section.
        ('{"version": 1, "workflow": {"soft_threshold": 3}}', "workflow", "soft_threshold"),
        ('{"version": 1, "review": {"enabled": false}}', "review", "enabled"),
        ('{"version": 1, "merge": {"allow_merge": true}}', "merge", "allow_merge"),
        ('{"version": 1, "local": {"max_review_rounds": 2}}', "local", "max_review_rounds"),
        ('{"version": 1, "github": {"cmd": "gh"}}', "github", "cmd"),
        # Top level: a section name misspelled, or a key that was never read.
        ('{"version": 1, "workflows": {"max_review_rounds": 3}}', "the top level", "workflows"),
        ('{"version": 1, "repository": "o/r"}', "the top level", "repository"),
        # A profile key, on a built-in profile and on a new one alike.
        ('{"version": 1, "profiles": {"fix": {"modle": "x"}}}', "profiles.fix", "modle"),
        ('{"version": 1, "profiles": {"extra": {"timeout": 5}}}', "profiles.extra", "timeout"),
    ],
)
def test_typos_from_the_issue_are_rejected(tmp_path, body, label, key):
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=f"unknown key.*under '{label}': {key} "):
        load_config_file(p)


def test_unknown_keys_rejected_in_yaml_and_toml_too(tmp_path):
    yaml = tmp_path / "cfg.yaml"
    yaml.write_text("version: 1\nworkflow:\n  max_review_round: 3\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="under 'workflow': max_review_round"):
        load_config_file(yaml)
    toml = tmp_path / "cfg.toml"
    toml.write_text("version = 1\n[review.replan]\nenable = false\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="under 'review.replan': enable"):
        load_config_file(toml)


def test_unknown_keys_rejected_on_both_yaml_backends(tmp_path, yaml_backend):
    p = tmp_path / "cfg.yaml"
    p.write_text("version: 1\nworkflow:\n  max_review_round: 3\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="under 'workflow': max_review_round"):
        load_config_file(p)


# A key written twice in one mapping. `json.loads`, PyYAML and the subset
# parser all keep the last value and drop the earlier one *before* the loader
# sees the document, so a typo in the dropped copy would be the very silent
# no-op the unknown-key check exists to refuse. `tomllib` refuses it itself.
@pytest.mark.parametrize(
    "text, key",
    [
        # The whole section repeated: the first copy, typo and all, is gone.
        (
            "version: 1\nworkflow:\n  max_review_round: 3\nworkflow:\n  max_review_rounds: 20\n",
            "workflow",
        ),
        # One leaf repeated: which bound is in force is decided by file order.
        (
            "version: 1\nworkflow:\n  max_review_rounds: 3\n  max_review_rounds: 20\n",
            "max_review_rounds",
        ),
        # A repeated profile name.
        (
            "version: 1\nprofiles:\n  fix:\n    model: a\n  fix:\n    model: b\n",
            "fix",
        ),
    ],
)
def test_duplicate_yaml_key_is_a_parse_error(tmp_path, yaml_backend, text, key):
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match=f"cannot parse config .*duplicate key '{key}'"):
        load_config_file(p)


def test_duplicate_json_key_is_a_parse_error(tmp_path):
    p = tmp_path / "cfg.json"
    p.write_text(
        '{"version": 1, "workflow": {"max_review_round": 3}, '
        '"workflow": {"max_review_rounds": 20}}',
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot parse config .*duplicate key 'workflow'"):
        load_config_file(p)
    p.write_text('{"version": 1, "safety": {"allow_merge": false, "allow_merge": true}}')
    with pytest.raises(ConfigurationError, match="duplicate key 'allow_merge'"):
        load_config_file(p)


def test_duplicate_toml_key_is_a_parse_error(tmp_path):
    p = tmp_path / "cfg.toml"
    p.write_text(
        "version = 1\n[workflow]\nmax_review_round = 3\n[workflow]\nmax_review_rounds = 20\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigurationError, match="cannot parse config .*twice"):
        load_config_file(p)


def test_yaml_same_key_in_different_mappings_is_not_a_duplicate(tmp_path, yaml_backend):
    """Only a key repeated within *one* mapping is a duplicate."""
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "version: 1\nprofiles:\n  fix:\n    model: a\n  analyze_execute:\n    model: b\n",
        encoding="utf-8",
    )
    cfg = load_config_file(p)
    assert cfg.profile("fix").model == "a" and cfg.profile("analyze_execute").model == "b"


def test_pyyaml_merge_key_override_is_not_a_duplicate(tmp_path):
    """``<<`` exists to be overridden; only PyYAML reads it, and it still may."""
    pytest.importorskip("yaml")
    p = tmp_path / "cfg.yaml"
    p.write_text(
        "version: 1\nprofiles:\n  fix: &base\n    model: a\n    effort: low\n"
        "  analyze_execute:\n    <<: *base\n    model: b\n",
        encoding="utf-8",
    )
    cfg = load_config_file(p)
    assert cfg.profile("analyze_execute").model == "b"
    assert cfg.profile("analyze_execute").effort == "low"


# PyYAML used to have its non-mapping root replaced by `{}` before the root
# check ran, so `false` or a list loaded as the built-in configuration while
# the subset parser refused the same file.
@pytest.mark.parametrize("text", ["false\n", "- item\n", "just a string\n", "42\n"])
def test_non_mapping_yaml_root_rejected_on_both_backends(tmp_path, yaml_backend, text):
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="mapping at top level|cannot parse"):
        load_config_file(p)


# The subset parser used to raise ConfigurationError itself, so its errors
# skipped the `cannot parse config <path>:` wrapper every other backend's
# parse error goes through and never said which file was at fault.
@pytest.mark.parametrize(
    "text, detail",
    [
        ("version: 1\n  safety:\n    allow_merge: false\n", "bad indentation at: 'safety:'"),
        ("version: 1\nsafety\n", "cannot parse line: 'safety'"),
        ("version: 1\nsafety:\n  allow_merge false\n", "cannot parse line: 'allow_merge false'"),
    ],
)
def test_yaml_subset_parse_errors_name_the_config_path(tmp_path, no_pyyaml, text, detail):
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    with pytest.raises(ConfigurationError) as excinfo:
        load_config_file(p)
    message = str(excinfo.value)
    assert message.startswith(f"cannot parse config {p}: YAML subset parser: {detail}")
    assert message.endswith("install PyYAML for full YAML support")


def test_yaml_subset_non_mapping_root_names_the_config_path(tmp_path, no_pyyaml):
    """A list root is refused by the same root check, and message, as under PyYAML."""
    p = tmp_path / "cfg.yaml"
    p.write_text("- item\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as excinfo:
        load_config_file(p)
    assert str(excinfo.value) == f"config {p} must contain a mapping at top level"


def test_yaml_parse_errors_name_the_config_path_on_both_backends(tmp_path, yaml_backend):
    """Whichever backend read the file, a malformed document is reported with its path."""
    p = tmp_path / "cfg.yaml"
    p.write_text("version: 1\n  safety:\n    allow_merge: false\n", encoding="utf-8")
    with pytest.raises(ConfigurationError) as excinfo:
        load_config_file(p)
    assert str(excinfo.value).startswith(f"cannot parse config {p}: ")


@pytest.mark.parametrize("text", ["", "# nothing but a comment\n", "\n\n"])
def test_empty_yaml_document_is_the_defaults(tmp_path, yaml_backend, text):
    """No content is the one non-mapping root that means "all defaults"."""
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    cfg = load_config_file(p)
    assert cfg.safety.allow_merge is False
    assert cfg.workflow.max_review_rounds == default_config().workflow.max_review_rounds


# PyYAML types its keys, so `1:` is the int 1 and `~:` is None. Such a name
# used to load into `dict[str, ProfileConfig]` and only fail later, as a raw
# `TypeError` when `doctor` sorted the names.
@pytest.mark.parametrize("name", ["1", "true", "~", "1.5"])
def test_non_string_pyyaml_profile_name_rejected(tmp_path, name):
    pytest.importorskip("yaml")
    p = tmp_path / "cfg.yaml"
    p.write_text(f"version: 1\nprofiles:\n  {name}:\n    provider: scripted\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="profile names must be non-empty strings"):
        load_config_file(p)


@pytest.mark.parametrize(
    "body",
    [
        '{"version": 1, "profiles": {"": {"provider": "scripted"}}}',
        '{"version": 1, "profiles": {"   ": {"provider": "scripted"}}}',
    ],
)
def test_blank_profile_name_rejected(tmp_path, body):
    p = tmp_path / "cfg.json"
    p.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigurationError, match="profile names must be non-empty strings"):
        load_config_file(p)


def test_quoted_numeric_profile_name_is_a_string(tmp_path, yaml_backend):
    p = tmp_path / "cfg.yaml"
    p.write_text('version: 1\nprofiles:\n  "1":\n    provider: scripted\n', encoding="utf-8")
    assert load_config_file(p).profile("1").provider == "scripted"


def test_removed_execution_allow_merge_keeps_its_own_message(tmp_path):
    """The deprecated gate key explains where it went instead of reading as a typo."""
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "execution": {"allow_merge": true, "bogus": 1}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="no longer supported") as info:
        load_config_file(p)
    assert "unknown key" not in str(info.value)


@pytest.mark.parametrize(
    "keys, config_cls",
    [
        (config.EXECUTION_KEYS, config.ExecutionConfig),
        (config.SAFETY_KEYS, config.SafetyConfig),
        (config.GITHUB_KEYS, config.GitHubConfig),
        (config.MERGE_KEYS, config.MergeConfig),
        (config.REVIEW_KEYS, config.ReviewConfig),
        (config.REPLAN_KEYS, config.ReplanConfig),
        (config.WORKFLOW_KEYS, config.WorkflowConfig),
        (config.LOCAL_KEYS, config.LocalConfig),
        (config.PROFILE_KEYS, config.ProfileConfig),
        (config.TOP_LEVEL_KEYS, config.AutoForgeConfig),
    ],
)
def test_known_key_tables_name_real_fields(keys, config_cls):
    """Every key the loader accepts lands on a field; nothing accepted is a no-op."""
    from dataclasses import fields

    names = {f.name for f in fields(config_cls)}
    assert set(keys) <= names, set(keys) - names
    # Fields the loader deliberately does not read from the file.
    derived = {"allow_merge_source", "name"}
    assert names - set(keys) <= derived, names - set(keys) - derived


def test_every_key_in_the_example_file_is_known(tmp_path):
    """The documented example is the reference config; a key it uses must load."""
    from pathlib import Path

    cfg = load_config_file(Path(__file__).resolve().parents[1] / "autoforge.example.yaml")
    assert cfg.workflow.max_review_rounds == 20 and cfg.local.max_workspace_entries == 50000


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
    from autoforge.profiles import REQUIRED_PROFILES

    validate_required_profiles(default_config(), REQUIRED_PROFILES)
    p = tmp_path / "cfg.json"
    p.write_text('{"version": 1, "profiles": {"review_round_1": {"model": ""}}}', encoding="utf-8")
    with pytest.raises(ConfigurationError, match="review_round_1"):
        validate_required_profiles(load_config_file(p), REQUIRED_PROFILES)


def test_opencode_command_shape():
    p = default_config().profile("review_round_1")
    argv = p.build_command("review this")
    assert argv[:2] == ["opencode", "run"] and "-m" in argv and "--variant" in argv
    assert argv[-2:] == ["--", "review this"]


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
    assert cfg.workflow.epic_update_every == 1
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
                    "epic_update_every": 3,
                },
                "review": {"replan": {"soft_threshold": 2, "hard_threshold": 3}},
            }
        ),
        encoding="utf-8",
    )
    wf = load_config_file(p).workflow
    assert (wf.max_review_rounds, wf.max_total_steps) == (3, 12)
    assert wf.epic_update_every == 3
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
        ('{"version": 1, "workflow": {"epic_update_every": 0}}', "epic_update_every.*>= 1"),
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


# -- agent environment allow-list and worktree location (#10) --------------------
def _execution_cfg(tmp_path, execution: dict):
    p = tmp_path / "cfg.json"
    p.write_text(json.dumps({"version": 1, "execution": execution}), encoding="utf-8")
    return load_config_file(p)


def test_default_environment_names_are_the_documented_allowlist():
    exe = default_config().execution
    assert exe.env_allowlist == list(config.DEFAULT_ENV_ALLOWLIST)
    assert exe.env_allowlist_extra == [] and exe.worktree_dir == ""
    names = exe.environment_names()
    assert names == tuple(config.DEFAULT_ENV_ALLOWLIST)
    assert len(names) == len(set(names))
    for required in ("PATH", "HOME", "GH_TOKEN", "GITHUB_TOKEN", "SSH_AUTH_SOCK", "LC_*"):
        assert required in names, required
    # Provider keys are contributed by the provider, not the controller default.
    assert not any(n.startswith(("ANTHROPIC", "OPENAI", "OPENCODE")) for n in names)


def test_env_allowlist_replaces_the_default_and_extra_adds_to_it(tmp_path):
    cfg = _execution_cfg(tmp_path, {"env_allowlist": ["PATH", "MY_*"]})
    assert cfg.execution.environment_names() == ("PATH", "MY_*")
    cfg = _execution_cfg(tmp_path, {"env_allowlist_extra": ["MY_TOOL_HOME", "PATH"]})
    names = cfg.execution.environment_names()
    assert names[: len(config.DEFAULT_ENV_ALLOWLIST)] == tuple(config.DEFAULT_ENV_ALLOWLIST)
    assert names[-1] == "MY_TOOL_HOME" and names.count("PATH") == 1


@pytest.mark.parametrize("key", ["env_allowlist", "env_allowlist_extra"])
@pytest.mark.parametrize("bad", ["A*B", "A-B", "1ABC", "A B", "*"])
def test_an_invalid_environment_pattern_is_a_configuration_error(tmp_path, key, bad):
    with pytest.raises(ConfigurationError, match=f"{key}.*not an environment variable name"):
        _execution_cfg(tmp_path, {key: ["PATH", bad]})


@pytest.mark.parametrize("key", ["env_allowlist", "env_allowlist_extra"])
@pytest.mark.parametrize("value", ["PATH", 1, {"a": 1}, ["PATH", 2], ["PATH", ""]])
def test_an_environment_list_must_be_a_list_of_strings(tmp_path, key, value):
    with pytest.raises(ConfigurationError, match=key):
        _execution_cfg(tmp_path, {key: value})


def test_an_empty_env_allowlist_is_rejected_not_an_empty_environment(tmp_path):
    with pytest.raises(ConfigurationError, match="env_allowlist.*at least one variable"):
        _execution_cfg(tmp_path, {"env_allowlist": []})
    # ...while an empty *extra* list is the default and fine.
    assert _execution_cfg(tmp_path, {"env_allowlist_extra": []}).execution.environment_names()


def test_worktree_dir_is_optional_and_a_string(tmp_path):
    assert _execution_cfg(tmp_path, {"worktree_dir": None}).execution.worktree_dir == ""
    assert _execution_cfg(tmp_path, {"worktree_dir": ""}).execution.worktree_dir == ""
    assert (
        _execution_cfg(tmp_path, {"worktree_dir": " ../af-worktrees "}).execution.worktree_dir
        == "../af-worktrees"
    )
    with pytest.raises(ConfigurationError, match="worktree_dir"):
        _execution_cfg(tmp_path, {"worktree_dir": 3})


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


# -- the two YAML backends must read the same file the same way (#40) -------
#
# An operator who installed `autoforge[yaml]` reads every `.yaml` config
# through PyYAML; one who did not reads it through the built-in subset
# parser. The file decides routing, loop bounds, timeouts and
# `safety.allow_merge`, and nothing downstream can notice that `on` was read
# as the string "on" on one backend and as `True` on the other, or `0600` as
# 600 here and 384 there. So the contract is not "both parse the example
# file": it is that whatever the subset parser accepts, PyYAML reads
# identically -- and what it cannot read that way it refuses outright, which
# reaches the operator as `cannot parse config <path>`.
#
# `subset accepted` => `PyYAML accepted, and the two Configs are equal`
#
# is the whole rule, in that one direction: the contrapositive also forbids
# the subset parser from quietly accepting a document PyYAML rejects.


def load_both_yaml_backends(path, monkeypatch):
    """``(pyyaml, subset)`` outcomes, each ``("ok", cfg)`` or ``("error", msg)``."""

    def outcome():
        try:
            return ("ok", load_config_file(path))
        except ConfigurationError as exc:
            return ("error", str(exc))

    with pyyaml_hidden(monkeypatch):
        subset = outcome()
    return outcome(), subset


def assert_backends_agree(path, monkeypatch):
    """The rule above, with the disagreement named when it is violated."""
    pyyaml, subset = load_both_yaml_backends(path, monkeypatch)
    text = path.read_text(encoding="utf-8")
    if subset[0] == "error":
        return  # refusing outright is the sanctioned way out
    assert pyyaml[0] == "ok", (
        f"the subset parser accepted a document PyYAML rejects:\n{text}\n{pyyaml[1]}"
    )
    assert subset[1] == pyyaml[1], (
        f"the two YAML backends read the same document differently:\n{text}"
    )


# Scalars the two backends used to resolve differently, grouped by why.
# `_scalar` is compared with PyYAML directly rather than through a Config,
# because a Config stringifies most of what it stores and would hide `1.0`
# (float) reading as `"1.0"` (string) on the other backend.
AMBIGUOUS_SCALARS = [
    # YAML 1.1 spells booleans in more ways than `true`/`false`, and only in
    # these casings: `on`/`off` are booleans, `TrUe` is a string.
    "yes", "no", "on", "off", "On", "OFF", "Yes", "NO",
    "true", "false", "True", "FALSE", "TrUe", "yEs", "y", "n",
    # Nulls, and the empty value a section header with everything commented
    # out produces.
    "null", "Null", "NULL", "~", "", "Nul",
    # Integers: binary, a leading zero (octal), hexadecimal, sexagesimal and
    # digit separators. `0600` is 384, `1:30` is 90 -- `int()` reads neither.
    "1", "0", "012", "0600", "-012", "+12", "1_000", "0_1",
    "0b101", "+0b11", "0b1_1", "0x1f", "-0x1f", "0x_1f", "0o17",
    "1:30", "-1:30", "12:00:00", "1:2:3", "60:00", "1_0:30", "1:60",
    # Floats: a dot is required, so `1e3` is a *string* in YAML 1.1.
    "1.0", "1.5", "-0.5", ".5", "1.", "1_0.5",
    "1e3", "1E3", "1.0e3", "1.0e+3", ".5e+3", "5.e+3",
    ".inf", "-.inf", ".nan", ".Inf", "1:30.5",
    # Timestamps: PyYAML builds a `datetime.date`, a type no config field
    # accepts, so the subset parser refuses rather than keeping the string.
    "2026-09-23", "2026-9-3", "20260923", "2026-09-23 10:00:00",
    # Quoted scalars, and the escapes the subset parser does not decode.
    "'1.0'", '"1.0"', "'yes'", '"on"', "''", '""', "'a''b'", '"a\\nb"', '"a" "b"', "'abc",
    # Plain scalars that merely look like something else.
    "hello", "hello world", "a:b", "?x", "-foo", "a,b", "a[b]", "a#b", "---", "0o17",
    # Flow sequences, including the nesting a comma split gets wrong.
    "[a, b]", "[1, 2]", "[]", "[a]", "[a, ]", "[a,,b]", '["x, y", z]',
    "[[1,2]]", "[a, [b, c]]", "{a: 1}", "{}",
    # Indicators that introduce YAML this parser does not implement.
    "&anchor", "*alias", "!!str 1", "!tag", "|", ">", "|-", "=", "<<",
    "}", "]", ",a", "%v", "@v", "`v",
]  # fmt: skip


@pytest.mark.parametrize("text", AMBIGUOUS_SCALARS)
def test_subset_scalar_agrees_with_pyyaml_or_refuses(text):
    """Every scalar the subset parser resolves, PyYAML resolves to the same value."""
    pyyaml = pytest.importorskip("yaml")
    try:
        ours = config._scalar(text)
    except ValueError as exc:
        assert "install PyYAML for full YAML support" in str(exc)
        return  # refusing outright is the sanctioned way out
    theirs = pyyaml.safe_load(f"k: {text}\n")["k"]
    # repr, not ==: it distinguishes 1 from True and 1.0, and NaN from itself.
    assert repr(ours) == repr(theirs), f"{text!r} reads as {ours!r} here and {theirs!r} there"


# Whole documents, so the comparison covers the parsed *Config* -- key typing,
# duplicate detection and section shape included -- not only one scalar.
AMBIGUOUS_DOCUMENTS = [
    # The merge gate written the four ways YAML 1.1 spells a boolean. `on`
    # used to open a gate under PyYAML and be the string "on" here.
    "version: 1\nsafety:\n  allow_merge: on\n",
    "version: 1\nsafety:\n  allow_merge: off\n",
    "version: 1\nsafety:\n  allow_merge: yes\n",
    "version: 1\nsafety:\n  allow_merge: TrUe\n",
    'version: 1\nsafety:\n  allow_merge: "false"\n',
    # Integers in the bases YAML 1.1 reads and Python does not.
    "version: 1\ngithub:\n  timeout_seconds: 0600\n",
    "version: 1\ngithub:\n  timeout_seconds: 0x1f\n",
    "version: 1\ngithub:\n  timeout_seconds: 1:30\n",
    "version: 1\ngithub:\n  timeout_seconds: 1_000\n",
    "version: 1\nworkflow:\n  max_review_rounds: 012\n",
    # `1.0` and `"1.0"` into a string field, plus the scalars that only look
    # numeric, and a timestamp (a `datetime.date` under PyYAML).
    "version: 1\nprofiles:\n  fix:\n    model: 1.0\n",
    'version: 1\nprofiles:\n  fix:\n    model: "1.0"\n',
    "version: 1\nprofiles:\n  fix:\n    model: 1e3\n",
    "version: 1\nprofiles:\n  fix:\n    model: .inf\n",
    "version: 1\nprofiles:\n  fix:\n    model: 2026-09-23\n",
    # An apostrophe in a plain scalar must not quote the rest of the line and
    # swallow the comment after it.
    "version: 1\nprofiles:\n  fix:\n    model: don't # the vendor spells it so\n",
    # Empty values: the omitted section, the explicit null, the empty string.
    "version: 1\nexecution:\n  worktree_dir:\n",
    "version: 1\nexecution:\n  worktree_dir: ~\n",
    'version: 1\nexecution:\n  worktree_dir: ""\n',
    "version: 1\nsafety:\n",
    "version: 1\nsafety:\n  # allow_merge: true\nmerge:\n",
    # A key repeated in one mapping: both backends keep the last value, so
    # both must refuse instead.
    "version: 1\nsafety:\n  allow_merge: false\n  allow_merge: true\n",
    "version: 1\nworkflow:\n  max_review_round: 3\nworkflow:\n  max_review_rounds: 20\n",
    # Keys are resolved too: `1:` is the integer 1 and `~:` is None in YAML,
    # and a profile name must be a non-empty string on either backend.
    "version: 1\nprofiles:\n  1:\n    provider: scripted\n",
    "version: 1\nprofiles:\n  ~:\n    provider: scripted\n",
    "version: 1\nprofiles:\n  true:\n    provider: scripted\n",
    'version: 1\nprofiles:\n  "1":\n    provider: scripted\n',
    # A colon inside a key or a value: `:` ends a key only before a space.
    "version: 1\nprofiles:\n  fix:\n    command: /usr/local/bin/claude\n",
    "version: 1\nprofiles:\n  fix:\n    model: openai/gpt-5.6-luna\n",
    "version: 1\nexecution:\n  worktree_dir: C:/tmp/worktrees\n",
    # Lists, block and flow, including the nesting a comma split gets wrong.
    'version: 1\nsafety:\n  required_checks: ["ci", "build"]\n',
    "version: 1\nsafety:\n  required_checks: []\n",
    "version: 1\nsafety:\n  required_checks:\n    - ci\n    - build\n",
    'version: 1\nmerge:\n  verification_commands: [["pytest", "-q"]]\n',
    "version: 1\nmerge:\n  verification_commands:\n    - - pytest\n      - -q\n",
    # A stray line the top-level block does not contain: the subset parser
    # used to return the part it had read and drop the rest, so a document
    # PyYAML rejects outright opened the merge gate on the other backend.
    "version: 1\nsafety:\n  allow_merge: true\n- ignored\n",
    "version: 1\nsafety:\n  allow_merge: true\nmore: 1\n- ignored\n",
    "- ignored\nversion: 1\nsafety:\n  allow_merge: true\n",
    "version: 1\n- ignored\n",
    # Tabs. PyYAML's scanner skips only spaces between tokens and ends a plain
    # scalar at a tab, so all of these are scanner errors there while the
    # subset parser's strip-and-split used to read them as plain scalars.
    "version: 1\nprofiles:\n  fix:\n    model: foo\t# the vendor spells it so\n",
    "version: 1\nprofiles:\n  fix:\n    model: foo\tbar\n",
    "version: 1\nprofiles:\n  fix:\n    model: foo\t\n",
    "version: 1\nprofiles:\n  fix:\n\tmodel: foo\n",
    "version:\t1\n",
    "version: 1\n\t\n",
    "version: 1\nsafety:\n  required_checks: [ci,\tbuild]\n",
    "version: 1\nsafety:\n  required_checks:\n    -\tci\n",
    # A tab *inside* a quoted scalar or a comment is legal under PyYAML, so
    # these must still load, and load to the same Config.
    'version: 1\nprofiles:\n  fix:\n    model: "a\tb"\n',
    "version: 1\nprofiles:\n  fix:\n    model: 'a\tb'\n",
    "version: 1\nprofiles:\n  fix:\n    model: foo # spelled\tso\n",
    "# a\tcomment\nversion: 1\nsafety:\n  allow_merge: true\n",
    'version: 1\nsafety:\n  required_checks: ["a\tb", ci]\n',
    # Roots that are not a mapping, and the empty document that means defaults.
    "false\n",
    "- item\n",
    "42\n",
    "",
    "# only a comment\n",
]


@pytest.mark.parametrize("text", AMBIGUOUS_DOCUMENTS)
def test_yaml_backends_read_the_same_document_the_same_way(tmp_path, monkeypatch, text):
    pytest.importorskip("yaml")
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    assert_backends_agree(p, monkeypatch)


# `assert_backends_agree` is satisfied by a refusal, so the two cases the
# subset parser used to accept get an explicit test that it now refuses them
# *and* that PyYAML refuses them too -- the differential pair, named.
@pytest.mark.parametrize(
    "text, detail",
    [
        (
            "version: 1\nsafety:\n  allow_merge: true\n- ignored\n",
            "unexpected content after the document at: '- ignored'",
        ),
        (
            "version: 1\nprofiles:\n  fix:\n    model: foo\t# c\n",
            "tab outside a quoted scalar at: '    model: foo\\t# c'",
        ),
    ],
)
def test_subset_backend_refuses_what_pyyaml_refuses(tmp_path, monkeypatch, text, detail):
    """Neither a partial document nor a tabbed plain scalar may load here."""
    pyyaml_module = pytest.importorskip("yaml")
    with pytest.raises(pyyaml_module.YAMLError):
        pyyaml_module.safe_load(text)  # the behaviour the subset parser must match
    p = tmp_path / "cfg.yaml"
    p.write_text(text, encoding="utf-8")
    pyyaml, subset = load_both_yaml_backends(p, monkeypatch)
    assert subset[0] == "error", f"the subset parser accepted {text!r}: {subset[1]}"
    assert subset[1].startswith(f"cannot parse config {p}: YAML subset parser: {detail}")
    assert pyyaml[0] == "error"


def test_tab_inside_a_quoted_scalar_still_loads_on_both_backends(tmp_path, monkeypatch):
    """The tab rule is PyYAML's, not a blanket ban: a quoted tab is legal there."""
    pytest.importorskip("yaml")
    p = tmp_path / "cfg.yaml"
    p.write_text('version: 1\nprofiles:\n  fix:\n    model: "a\tb"\n', encoding="utf-8")
    pyyaml, subset = load_both_yaml_backends(p, monkeypatch)
    assert pyyaml[0] == "ok" and subset[0] == "ok", (pyyaml, subset)
    assert subset[1].profile("fix").model == "a\tb"
    assert subset[1] == pyyaml[1]


def test_example_config_is_the_same_on_both_yaml_backends(monkeypatch):
    """The repository's own example file, the one config every operator starts from."""
    pytest.importorskip("yaml")
    example = Path(__file__).resolve().parents[1] / "autoforge.example.yaml"
    assert example.exists(), f"example config missing: {example}"
    assert_backends_agree(example, monkeypatch)
    pyyaml, subset = load_both_yaml_backends(example, monkeypatch)
    # Not vacuously true: the example must load, not merely fail on both.
    assert pyyaml[0] == "ok" and subset[0] == "ok"


def test_dev_group_keeps_pyyaml_so_ci_exercises_the_pyyaml_branch():
    """The extra is a supported config path, so something must install it.

    `_load_yaml` branches on whether PyYAML imports, and the tests above skip
    their PyYAML half when it does not. CI installs `--group dev` and nothing
    else, so dropping PyYAML from that group would retire this whole file's
    PyYAML coverage silently, leaving the branch an operator gets from
    `pip install autoforge[yaml]` exercised by no environment (#40).
    """
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    dev = [str(d).lower() for d in data["dependency-groups"]["dev"]]
    assert any(d.startswith("pyyaml") for d in dev), (
        f"pyyaml must stay in the dev dependency group, got {dev}"
    )
    assert "yaml" in data["project"]["optional-dependencies"], (
        "the `yaml` extra is what the PyYAML branch exists for"
    )


# The three classes above, spelled out against the keys they actually decide,
# so what changed is legible without reconstructing it from a corpus entry.
@pytest.mark.parametrize(
    "written, expected",
    [
        ("on", True), ("On", True), ("ON", True), ("yes", True), ("true", True),
        ("off", False), ("OFF", False), ("no", False), ("false", False),
    ],
)  # fmt: skip
def test_yaml_booleans_decide_the_merge_gate_alike_on_both_backends(
    tmp_path, yaml_backend, written, expected
):
    """``safety.allow_merge: on`` is a boolean in YAML 1.1 -- on either backend.

    The subset parser used to keep `on`/`off` as the strings they look like,
    which `_as_bool` then refused: the same file opened the merge gate with
    the `yaml` extra installed and failed to load without it.
    """
    p = tmp_path / "cfg.yaml"
    p.write_text(f"version: 1\nsafety:\n  allow_merge: {written}\n", encoding="utf-8")
    assert load_config_file(p).safety.allow_merge is expected


@pytest.mark.parametrize("written", ["TrUe", "yEs", "oN", "nO", "y", "n"])
def test_miscased_yaml_booleans_are_strings_on_both_backends(tmp_path, yaml_backend, written):
    """Only the casings YAML 1.1 lists are booleans; the rest stay strings.

    `_as_bool` refuses a string, so the gate stays closed on both backends
    rather than opening on the one that lower-cased before comparing.
    """
    p = tmp_path / "cfg.yaml"
    p.write_text(f"version: 1\nsafety:\n  allow_merge: {written}\n", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="safety.allow_merge"):
        load_config_file(p)


@pytest.mark.parametrize(
    "written, expected",
    [("600", 600), ("0600", 384), ("0x1f", 31), ("0b101", 5), ("1:30", 90), ("1_000", 1000)],
)
def test_yaml_integer_bases_read_alike_on_both_backends(tmp_path, yaml_backend, written, expected):
    """A leading zero is octal in YAML 1.1, and `1:30` is sexagesimal.

    Surprising, but it is what an operator with the extra already gets, so
    the subset parser reads them the same way instead of calling `0600` six
    hundred and handing the controller a different timeout.
    """
    p = tmp_path / "cfg.yaml"
    p.write_text(f"version: 1\ngithub:\n  timeout_seconds: {written}\n", encoding="utf-8")
    assert load_config_file(p).github.timeout_seconds == expected
