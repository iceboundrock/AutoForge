"""The configuration a REMOTE run was recorded under (#219).

A REMOTE run records, when it is created, the profile routing (provider,
model, effort, limits) and the agent worktree root it runs on, and every
later ``resume``, ``step`` and ``unblock`` compares its own with it before
any phase executes (:mod:`autoforge.config_fingerprint`,
:meth:`ControllerEngine.check_run_configuration`). The CLI end to end, with
the scripted agent and the fake GitHub, is in ``tests/test_cli.py``; the
state boundary (protocol 9) in ``tests/test_state.py``.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
from collections.abc import Callable
from pathlib import Path

import pytest

from autoforge.config import AutoForgeConfig, ProfileConfig, default_config
from autoforge.config_fingerprint import (
    _PROFILE_KEYS,
    FINGERPRINT_PROFILES,
    FINGERPRINT_SCHEMA,
    SOURCE_ACCEPTED,
    SOURCE_CREATED,
    SOURCE_UPGRADED,
    WORKTREE_ROOT_KEY,
    ConfigFingerprint,
    ProfileRouting,
    RecordedConfiguration,
    fingerprint_routing,
)
from autoforge.engine import ControllerEngine
from autoforge.errors import (
    ConfigurationDriftError,
    ConfigurationError,
    StateError,
    VerificationError,
)
from autoforge.profiles import REQUIRED_PROFILES
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.state import load_state

from .conftest import FakeGitHub, make_engine

ROOT = "/srv/worktrees"


def _fingerprint(cfg: AutoForgeConfig | None = None, root: str = ROOT) -> ConfigFingerprint:
    return ConfigFingerprint.from_config(cfg or default_config(), root)


def _recorded(**over) -> dict:
    record = RecordedConfiguration(
        fingerprint=_fingerprint(),
        config_file="/home/me/autoforge.yaml",
        source=SOURCE_CREATED,
        recorded_at="2026-10-14T00:00:00+00:00",
    ).to_dict()
    record.update(over)
    return record


# -- the value: what is recorded, and nothing else ---------------------------------


def test_every_profile_a_remote_run_routes_to_is_recorded():
    """The required profiles and update_epic, which the run reaches after a merge."""
    assert FINGERPRINT_PROFILES == (*REQUIRED_PROFILES, "update_epic")
    fp = _fingerprint()
    assert tuple(fp.profiles) == FINGERPRINT_PROFILES
    assert fp.profiles["analyze_execute"] == ProfileRouting(
        provider="claude",
        model="fable",
        effort="high",
        idle_timeout_seconds=900,
        max_runtime_seconds=None,
    )
    assert fp.worktree_root == ROOT


def test_an_unconfigured_profile_is_recorded_as_none():
    cfg = default_config()
    del cfg.profiles["update_epic"]
    assert _fingerprint(cfg).profiles["update_epic"] is None


def test_limits_are_recorded_as_the_values_the_agent_runs_under():
    """A profile's own limit wins; otherwise the execution default applies."""
    cfg = default_config()
    cfg.execution.default_idle_timeout_seconds = 1200
    cfg.execution.default_max_runtime_seconds = 7200
    cfg.profiles["fix"].idle_timeout_seconds = 300
    fp = _fingerprint(cfg)
    assert fp.profiles["fix"].idle_timeout_seconds == 300
    assert fp.profiles["fix"].max_runtime_seconds == 7200
    assert fp.profiles["analyze_execute"].idle_timeout_seconds == 1200


def test_a_profile_that_could_not_run_is_still_recorded():
    """Recording refuses only what its record could not hold (below): Claude's
    text output without a ceiling is a configuration error at launch
    (``agent_limits``), not at record time."""
    cfg = default_config()
    cfg.profiles["update_epic"] = ProfileConfig(
        name="update_epic", provider="claude", model="fable", options={"output_format": "text"}
    )
    with pytest.raises(ConfigurationError):
        cfg.agent_limits(cfg.profiles["update_epic"])
    assert _fingerprint(cfg).profiles["update_epic"].max_runtime_seconds is None


# One mutation per recorded profile key: the test below holds the table to
# ``_PROFILE_KEYS``, so a key added without a row fails here.
PROFILE_MUTATIONS: dict[str, tuple[Callable[[ProfileConfig], None], str, str]] = {
    "provider": (lambda p: setattr(p, "provider", "pi"), '"claude"', '"pi"'),
    "model": (lambda p: setattr(p, "model", "opus"), '"fable"', '"opus"'),
    "effort": (lambda p: setattr(p, "effort", "low"), '"high"', '"low"'),
    "idle_timeout_seconds": (lambda p: setattr(p, "idle_timeout_seconds", 3600), "900", "3600"),
    "max_runtime_seconds": (lambda p: setattr(p, "max_runtime_seconds", 600), "null", "600"),
}


def test_the_mutation_table_covers_every_recorded_profile_key():
    assert sorted(PROFILE_MUTATIONS) == sorted(_PROFILE_KEYS)


@pytest.mark.parametrize("key", sorted(PROFILE_MUTATIONS))
def test_each_recorded_profile_key_is_compared_and_named(key):
    mutate, before, after = PROFILE_MUTATIONS[key]
    cfg = default_config()
    mutate(cfg.profiles["fix"])
    [change] = _fingerprint().changes(_fingerprint(cfg))
    assert change.key == f"profiles.fix.{key}"
    assert (change.recorded, change.current) == (before, after)
    assert change.line() == f"profiles.fix.{key}: {before} -> {after}"


def test_the_worktree_root_is_compared_and_named():
    [change] = _fingerprint().changes(_fingerprint(root="/elsewhere"))
    assert change.key == WORKTREE_ROOT_KEY
    assert change.line() == f'{WORKTREE_ROOT_KEY}: "{ROOT}" -> "/elsewhere"'


def test_a_profile_configured_on_one_side_only_is_one_change():
    cfg = default_config()
    del cfg.profiles["update_epic"]
    [change] = _fingerprint().changes(_fingerprint(cfg))
    assert change.key == "profiles.update_epic"
    assert change.current == "(not configured)" and '"provider": "opencode"' in change.recorded


def test_changes_are_listed_in_a_stable_order():
    cfg = default_config()
    cfg.profiles["review_round_6_plus"].model = "x"
    cfg.profiles["analyze_execute"].effort = "low"
    cfg.profiles["analyze_execute"].model = "y"
    keys = [c.key for c in _fingerprint().changes(_fingerprint(cfg, root="/other"))]
    assert keys == [
        "profiles.analyze_execute.model",
        "profiles.analyze_execute.effort",
        "profiles.review_round_6_plus.model",
        WORKTREE_ROOT_KEY,
    ]


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda c: setattr(c.profiles["fix"], "command", "/opt/claude"), id="command"),
        pytest.param(
            lambda c: c.profiles["fix"].extra_args.append("--api-key=sk-ant-x"), id="extra_args"
        ),
        pytest.param(
            lambda c: c.profiles["fix"].options.update(permission_mode="plan"), id="options"
        ),
        pytest.param(lambda c: c.profiles.update(extra=ProfileConfig("extra", "pi")), id="unused"),
        pytest.param(lambda c: setattr(c.workflow, "max_total_steps", 9), id="step_budget"),
        pytest.param(lambda c: setattr(c.sandbox, "mode", "required"), id="sandbox_mode"),
        pytest.param(lambda c: c.execution.env_allowlist_extra.append("FOO"), id="env"),
        pytest.param(lambda c: setattr(c.safety, "allow_merge", True), id="merge_gate"),
        pytest.param(
            lambda c: setattr(c.execution, "command_timeout_seconds", 60), id="command_timeout"
        ),
    ],
)
def test_keys_outside_the_fingerprint_may_change_between_steps(mutate):
    """Only routing and the worktree root are recorded: a key that could hold a
    secret (command, arguments, options) is neither stored nor compared, and
    a bound, a gate or the sandbox mode may change between steps."""
    cfg = default_config()
    mutate(cfg)
    assert _fingerprint().changes(_fingerprint(cfg)) == []
    stored = json.dumps(RecordedConfiguration(_fingerprint(cfg), "", SOURCE_CREATED, "t").to_dict())
    assert (
        "sk-ant" not in stored and "/opt/claude" not in stored and "permission_mode" not in stored
    )


# -- the persisted form: closed, strict, never migrated ---------------------------


def test_the_recorded_configuration_round_trips():
    record = _recorded()
    parsed = RecordedConfiguration.from_dict(copy.deepcopy(record))
    assert parsed.to_dict() == record
    assert parsed.fingerprint == _fingerprint()
    assert parsed.origin() == "--config /home/me/autoforge.yaml"
    assert RecordedConfiguration.from_dict(_recorded(config_file="")).origin() == (
        "the built-in defaults"
    )


def _profile(record: dict, **over) -> dict:
    record["profiles"]["fix"].update(over)
    return record


@pytest.mark.parametrize(
    ("record", "needle"),
    [
        (_recorded(schema=2), "schema 2 is not 1"),
        ({k: v for k, v in _recorded().items() if k != "schema"}, "schema None is not 1"),
        ({**_recorded(), "extra": 1}, "must hold exactly"),
        ({k: v for k, v in _recorded().items() if k != "source"}, "must hold exactly"),
        (_recorded(profiles={}), "profiles must hold exactly"),
        (_recorded(source="guessed"), "source must be one of"),
        (_recorded(worktree_root="relative/dir"), "worktree_root must be an absolute path"),
        (_recorded(worktree_root=""), "worktree_root must be a non-empty string"),
        (_recorded(config_file="autoforge.yaml"), "config_file must be an absolute path"),
        (_recorded(config_file=None), "config_file must be a string"),
        (_recorded(recorded_at=""), "recorded_at must be a non-empty string"),
        (_profile(_recorded(), provider=""), "profiles.fix.provider must be a non-empty"),
        (_profile(_recorded(), model=7), "profiles.fix.model must be a string"),
        (_profile(_recorded(), effort="x" * 600), "profiles.fix.effort must be a string"),
        (_profile(_recorded(), idle_timeout_seconds=None), "must be a positive integer"),
        (_profile(_recorded(), idle_timeout_seconds=True), "must be a positive integer"),
        (_profile(_recorded(), max_runtime_seconds=0), "a positive integer or null"),
        (_profile(_recorded(), extra="x"), "profiles.fix must hold exactly"),
        ([], "must hold exactly"),
    ],
)
def test_a_malformed_record_is_refused(record, needle):
    with pytest.raises(StateError) as exc:
        RecordedConfiguration.from_dict(record)
    assert needle in str(exc.value)


# -- what can be recorded: what the record reads back (R1-F1) ---------------------

# Values the configuration accepts and the record's parser does not: recorded
# as they are, the run's state file would never load again. ``update_epic``
# is no required profile, so ``validate_required_profiles`` checks nothing of
# it before the run reaches it.
UNRECORDABLE: dict[str, tuple[Callable[[AutoForgeConfig], None], str]] = {
    "a model over the bound": (
        lambda c: setattr(c.profiles["fix"], "model", "m" * 513),
        "profiles.fix.model must be a string of at most 512 characters",
    ),
    "an effort over the bound": (
        lambda c: setattr(c.profiles["review_round_1"], "effort", "e" * 513),
        "profiles.review_round_1.effort must be a string of at most 512 characters",
    ),
    "an empty update_epic provider": (
        lambda c: setattr(c.profiles["update_epic"], "provider", ""),
        "profiles.update_epic.provider must be a non-empty string",
    ),
    "a zero update_epic idle limit": (
        lambda c: setattr(c.profiles["update_epic"], "idle_timeout_seconds", 0),
        "profiles.update_epic.idle_timeout_seconds must be a positive integer",
    ),
    "a negative update_epic ceiling": (
        lambda c: setattr(c.profiles["update_epic"], "max_runtime_seconds", -1),
        "profiles.update_epic.max_runtime_seconds must be a positive integer or null",
    ),
}


def _unrecordable(case: str) -> AutoForgeConfig:
    cfg = default_config()
    UNRECORDABLE[case][0](cfg)
    return cfg


@pytest.mark.parametrize("case", sorted(UNRECORDABLE))
def test_a_value_the_record_could_not_hold_is_refused_by_name(case):
    cfg = _unrecordable(case)
    for build in (lambda: _fingerprint(cfg), lambda: fingerprint_routing(cfg)):
        with pytest.raises(ConfigurationError) as exc:
            build()
        message = str(exc.value)
        assert message.startswith(UNRECORDABLE[case][1])
        assert message.endswith(
            ": a REMOTE run records it in its state file, which could not be loaded again "
            "otherwise (#219)"
        )
        assert "m" * 513 not in message and "e" * 513 not in message  # the key, never the value


def test_a_model_at_the_bound_is_recorded_and_reads_back():
    cfg = default_config()
    cfg.profiles["fix"].model = "m" * 512
    record = RecordedConfiguration(
        fingerprint=_fingerprint(cfg),
        config_file="/home/me/autoforge.yaml",
        source=SOURCE_CREATED,
        recorded_at="2026-10-14T00:00:00+00:00",
    )
    parsed = RecordedConfiguration.from_dict(copy.deepcopy(record.persisted()))
    assert parsed.fingerprint.profiles["fix"].model == "m" * 512
    assert parsed == record


def test_a_worktree_root_the_record_could_not_hold_is_refused():
    with pytest.raises(ConfigurationError, match="must be a non-empty string of at most 4096"):
        _fingerprint(root="/" + "w" * 4096)


@pytest.mark.parametrize(
    ("config_file", "needle"),
    [
        ("autoforge.yaml", "config_file must be an absolute path"),
        ("/" + "c" * 4096, "config_file must be a string of at most 4096 characters"),
    ],
)
def test_a_record_is_persisted_only_when_it_reads_back(config_file, needle):
    record = RecordedConfiguration(
        fingerprint=_fingerprint(),
        config_file=config_file,
        source=SOURCE_CREATED,
        recorded_at="2026-10-14T00:00:00+00:00",
    )
    with pytest.raises(ConfigurationError, match=needle):
        record.persisted()


# -- the gate: an engine per invocation, on the same run ---------------------------


def _created(tmp_path, cfg: AutoForgeConfig | None = None) -> ControllerEngine:
    """A REMOTE run created under ``cfg``, its configuration recorded and saved."""
    eng = make_engine(tmp_path / ".autoforge", cfg=cfg)
    eng.config_file = "/home/me/autoforge.yaml"
    eng.cli_global_flags = ("--config", "autoforge.yaml", "--state-dir", "sd")
    eng.record_run_configuration()
    eng.save()
    return eng


def _invocation(
    eng: ControllerEngine, cfg: AutoForgeConfig, *, config_file: str = "", workdir=None
) -> ControllerEngine:
    """A later command on ``eng``'s run, under ``cfg``, with the run loaded."""
    provider = ScriptedProvider(None)
    later = ControllerEngine(
        config=cfg,
        state_dir=eng.paths.state_dir,
        workdir=workdir or eng.workdir,
        github=FakeGitHub(),
        providers=ProviderRegistry(overrides={"claude": provider, "opencode": provider}),
    )
    later.config_file = config_file
    # No --state-dir: this command found the run through its configuration's
    # state_dir, and the restore command still names the directory.
    later.cli_global_flags = ()
    later.load()
    return later


def _sd(eng: ControllerEngine) -> str:
    """``eng``'s state directory as a restore command names it: absolute."""
    return os.path.abspath(eng.paths.state_dir)


def _events(eng: ControllerEngine) -> list[dict]:
    path = eng.paths.logs_dir / eng.state.run_id / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_run_records_the_configuration_it_was_created_under(tmp_path):
    eng = _created(tmp_path)
    recorded = RecordedConfiguration.from_dict(load_state(eng.paths.state_file).config_fingerprint)
    assert recorded.source == SOURCE_CREATED
    assert recorded.config_file == "/home/me/autoforge.yaml"
    common = Path(os.path.realpath(Path(eng.workdir) / ".git"))
    assert recorded.fingerprint.worktree_root == str(common / "autoforge" / "worktrees")
    assert recorded.fingerprint == _fingerprint(
        default_config(), recorded.fingerprint.worktree_root
    )
    assert eng.agent_worktree_path() == Path(recorded.fingerprint.worktree_root) / "2"


def test_the_same_configuration_passes_with_nothing_to_say(tmp_path):
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    later = _invocation(eng, default_config())
    assert later.check_run_configuration(("resume",)) == ""
    assert eng.paths.state_file.read_bytes() == before


@pytest.mark.parametrize("key", sorted(PROFILE_MUTATIONS))
def test_a_changed_profile_key_refuses_and_writes_nothing(tmp_path, key):
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    cfg = default_config()
    PROFILE_MUTATIONS[key][0](cfg.profiles["review_round_2_5"])
    later = _invocation(eng, cfg)
    with pytest.raises(ConfigurationDriftError) as exc:
        later.check_run_configuration(("resume",))
    message = str(exc.value)
    assert f"\n  profiles.review_round_2_5.{key}: " in message
    assert "refuses before any phase executes; nothing was written" in message
    restore = shlex.join(
        ["autoforge", "--config", "/home/me/autoforge.yaml", "--state-dir", _sd(eng), "resume"]
    )
    assert f"To continue under the recorded configuration: {restore}\n" in message
    assert "repeat it with --accept-config-change" in message
    assert eng.paths.state_file.read_bytes() == before
    assert _events(later) == []


def test_a_changed_worktree_root_refuses(tmp_path, tmp_path_factory):
    eng = _created(tmp_path)
    cfg = default_config()
    elsewhere = tmp_path_factory.mktemp("worktrees")
    cfg.execution.worktree_dir = str(elsewhere)
    with pytest.raises(ConfigurationDriftError) as exc:
        _invocation(eng, cfg).check_run_configuration(("step",))
    assert f'{WORKTREE_ROOT_KEY}: "' in str(exc.value)
    assert f'" -> "{os.path.realpath(elsewhere)}"' in str(exc.value)
    assert "'step' refuses" in str(exc.value)


def test_a_relative_worktree_dir_is_resolved_from_the_command_directory(tmp_path):
    """The same configuration run from another directory puts the worktrees
    elsewhere, which is a change of the recorded root."""
    cfg = default_config()
    cfg.execution.worktree_dir = "../wt"
    eng = _created(tmp_path, cfg)
    sub = Path(eng.workdir) / "sub"
    sub.mkdir()
    later = _invocation(eng, cfg, config_file="/home/me/autoforge.yaml", workdir=sub)
    with pytest.raises(ConfigurationDriftError) as exc:
        later.check_run_configuration(("resume",))
    message = str(exc.value)
    assert "This command loads the same file, /home/me/autoforge.yaml" in message
    assert "runs from another directory or repository" in message
    assert "To continue under the recorded configuration:" not in message


def test_a_run_recorded_from_the_built_in_defaults_is_restored_without_config(tmp_path):
    eng = make_engine(tmp_path / ".autoforge")
    eng.record_run_configuration()
    eng.save()
    cfg = default_config()
    cfg.profiles["fix"].model = "opus"
    later = _invocation(eng, cfg, config_file="/home/me/other.yaml")
    with pytest.raises(ConfigurationDriftError) as exc:
        later.check_run_configuration(("resume",))
    message = str(exc.value)
    assert "was recorded under the built-in defaults (created " in message
    restore = shlex.join(["autoforge", "--state-dir", _sd(eng), "resume"])
    assert f"To continue under the recorded configuration: {restore}\n" in message


def test_accepting_a_change_logs_it_then_records_it(tmp_path):
    eng = _created(tmp_path)
    cfg = default_config()
    cfg.profiles["fix"].idle_timeout_seconds = 3600
    later = _invocation(eng, cfg, config_file="/home/me/raised.yaml")
    notice = later.check_run_configuration(("resume",), accept_change=True)
    assert notice.startswith("accepted the configuration change of run ")
    assert "--config /home/me/raised.yaml is the run's configuration from now on" in notice
    assert "\n  profiles.fix.idle_timeout_seconds: 900 -> 3600" in notice
    saved = load_state(eng.paths.state_file)
    recorded = RecordedConfiguration.from_dict(saved.config_fingerprint)
    assert recorded.source == SOURCE_ACCEPTED and recorded.config_file == "/home/me/raised.yaml"
    assert recorded.fingerprint.profiles["fix"].idle_timeout_seconds == 3600
    assert saved.phase == eng.state.phase and saved.step_count == eng.state.step_count
    [event] = _events(later)
    assert event["phase"] == "INITIALIZING-config-change"
    assert event["profile"] == "(operator configuration change; no agent)"
    assert event["metadata"]["command"] == "resume" and event["metadata"]["accepted"] is True
    assert event["metadata"]["changes"] == [
        {"key": "profiles.fix.idle_timeout_seconds", "recorded": "900", "current": "3600"}
    ]
    assert event["metadata"]["recorded_config_file"] == "/home/me/autoforge.yaml"
    assert event["metadata"]["config_file"] == "/home/me/raised.yaml"
    # The accepted configuration is the run's: the next command passes under
    # it and refuses the one the run was created under.
    assert _invocation(eng, cfg).check_run_configuration(("resume",)) == ""
    with pytest.raises(ConfigurationDriftError):
        _invocation(eng, default_config()).check_run_configuration(("resume",))


def test_a_refused_log_write_accepts_nothing(tmp_path, monkeypatch):
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    cfg = default_config()
    cfg.profiles["fix"].model = "opus"
    later = _invocation(eng, cfg)

    def refuse(self, record, *a, **k):
        raise StateError("run log refused")

    monkeypatch.setattr("autoforge.runlog.RunLogger.log_execution", refuse)
    with pytest.raises(StateError, match="run log refused"):
        later.check_run_configuration(("resume",), accept_change=True)
    assert eng.paths.state_file.read_bytes() == before


def test_a_dry_run_reports_and_writes_nothing(tmp_path):
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    cfg = default_config()
    cfg.profiles["fix"].effort = "low"
    later = _invocation(eng, cfg)
    with pytest.raises(ConfigurationDriftError):
        later.check_run_configuration(("resume",), dry_run=True)
    notice = later.check_run_configuration(("resume",), accept_change=True, dry_run=True)
    assert notice.startswith("[dry-run] would accept the configuration change of run ")
    assert '\n  profiles.fix.effort: "high" -> "low"' in notice
    assert eng.paths.state_file.read_bytes() == before
    assert _events(later) == []


def test_a_dry_run_outside_a_checkout_compares_the_routing_and_says_so(tmp_path, tmp_path_factory):
    """A dry run may be planned outside any repository. The default root is a
    git read there, so it is left out and named rather than guessed; the
    command itself, which needs the checkout, compares it."""
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    elsewhere = tmp_path_factory.mktemp("elsewhere")  # outside the run's repository
    same = _invocation(eng, default_config(), workdir=elsewhere)
    notice = same.check_run_configuration(("resume",), dry_run=True)
    assert notice.startswith("[dry-run] the agent worktree root was not compared: ")
    cfg = default_config()
    cfg.profiles["fix"].model = "opus"
    changed = _invocation(eng, cfg, workdir=elsewhere)
    with pytest.raises(ConfigurationDriftError) as exc:
        changed.check_run_configuration(("resume",), dry_run=True)
    assert '\n  profiles.fix.model: "fable" -> "opus"' in str(exc.value)
    assert WORKTREE_ROOT_KEY not in str(exc.value)
    assert str(exc.value).endswith(notice)
    with pytest.raises(VerificationError, match="cannot locate the git directory"):
        same.check_run_configuration(("resume",))
    assert eng.paths.state_file.read_bytes() == before
    assert _events(same) == []


def test_a_run_without_a_recorded_configuration_is_not_checked_and_records_one(tmp_path):
    """A state file written before #219 loads and resumes; the command records
    the configuration it runs on, saved with its next save."""
    eng = make_engine(tmp_path / ".autoforge")
    eng.save()
    assert load_state(eng.paths.state_file).config_fingerprint == {}
    cfg = default_config()
    cfg.profiles["fix"].model = "opus"
    later = _invocation(eng, cfg, config_file="/home/me/autoforge.yaml")
    dry = later.check_run_configuration(("resume",), dry_run=True)
    assert dry.startswith("[dry-run] run ") and "it is not checked" in dry
    assert later.state.config_fingerprint == {}
    notice = later.check_run_configuration(("resume",))
    assert "had no recorded configuration" in notice
    assert "--config /home/me/autoforge.yaml is recorded as the run's" in notice
    assert load_state(eng.paths.state_file).config_fingerprint == {}  # not saved yet
    later.save()
    recorded = RecordedConfiguration.from_dict(load_state(eng.paths.state_file).config_fingerprint)
    assert recorded.source == SOURCE_UPGRADED
    assert recorded.fingerprint.profiles["fix"].model == "opus"
    with pytest.raises(ConfigurationDriftError):
        _invocation(eng, default_config()).check_run_configuration(("resume",))


@pytest.mark.parametrize("case", sorted(UNRECORDABLE))
def test_a_run_refuses_a_configuration_it_could_not_record_before_any_state(tmp_path, case):
    """``run`` validates the configuration before it creates the run, and
    recording it refuses too: no state file is written that could not load."""
    eng = make_engine(tmp_path / ".autoforge", cfg=_unrecordable(case))
    with pytest.raises(ConfigurationError, match=UNRECORDABLE[case][1]):
        eng.validate_config()
    with pytest.raises(ConfigurationError, match=UNRECORDABLE[case][1]):
        eng.record_run_configuration()
    assert eng.state.config_fingerprint == {}
    assert not eng.paths.state_file.exists()


def test_a_model_at_the_bound_is_recorded_and_the_run_loads_again(tmp_path):
    cfg = default_config()
    cfg.profiles["fix"].model = "m" * 512
    eng = _created(tmp_path, cfg)
    saved = load_state(eng.paths.state_file)
    assert saved.config_fingerprint["profiles"]["fix"]["model"] == "m" * 512
    assert _invocation(eng, cfg).check_run_configuration(("resume",)) == ""


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_upgrade_refuses_a_configuration_it_could_not_record(tmp_path, dry_run):
    """A run from before #219 is not checked, but the configuration its next
    command would record must read back: refused, with nothing recorded."""
    eng = make_engine(tmp_path / ".autoforge")
    eng.save()
    before = eng.paths.state_file.read_bytes()
    later = _invocation(eng, _unrecordable("a model over the bound"))
    with pytest.raises(ConfigurationError, match="profiles.fix.model must be a string"):
        later.check_run_configuration(("resume",), dry_run=dry_run)
    assert later.state.config_fingerprint == {}
    assert eng.paths.state_file.read_bytes() == before


@pytest.mark.parametrize("dry_run", [False, True])
def test_an_accepted_change_the_record_could_not_hold_is_refused_unlogged(tmp_path, dry_run):
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    later = _invocation(eng, _unrecordable("a model over the bound"))
    with pytest.raises(ConfigurationError, match="profiles.fix.model must be a string"):
        later.check_run_configuration(("resume",), accept_change=True, dry_run=dry_run)
    assert eng.paths.state_file.read_bytes() == before
    assert _events(later) == []


def test_an_accepted_change_whose_config_path_could_not_be_recorded_logs_nothing(tmp_path):
    eng = _created(tmp_path)
    before = eng.paths.state_file.read_bytes()
    cfg = default_config()
    cfg.profiles["fix"].model = "opus"
    later = _invocation(eng, cfg, config_file="/" + "c" * 4096)
    with pytest.raises(ConfigurationError, match="config_file must be a string of at most 4096"):
        later.check_run_configuration(("resume",), accept_change=True)
    assert eng.paths.state_file.read_bytes() == before
    assert _events(later) == []


def test_the_schema_is_pinned():
    """Bumping it is a deliberate change: another schema is refused, never migrated."""
    assert FINGERPRINT_SCHEMA == 1
