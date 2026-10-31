"""The ``sandbox.mode`` a run is pinned to across its commands (#280).

A run records the mode it is created under, and every later ``resume``,
``step`` and ``unblock`` compares its own with it before any phase executes
(:mod:`autoforge.sandbox_pin`,
:meth:`ControllerEngine.check_run_configuration`): a weaker mode is refused
unless the operator accepts it, which the run log records first; a stronger
one is pinned. The CLI end to end is in ``tests/test_cli.py``; the state
boundary (protocol 12) in ``tests/test_state.py``; where a configuration
file may lie in ``tests/test_sandbox_policy.py``.
"""

from __future__ import annotations

import json

import pytest

from autoforge.config import (
    SANDBOX_DISABLED,
    SANDBOX_PREFERRED,
    SANDBOX_REQUIRED,
    AutoForgeConfig,
    default_config,
)
from autoforge.engine import ControllerEngine
from autoforge.errors import ConfigurationDriftError, StateError
from autoforge.providers import ProviderRegistry, ScriptedProvider
from autoforge.sandbox_pin import (
    PIN_SCHEMA,
    SOURCE_ACCEPTED,
    SOURCE_CREATED,
    SOURCE_STRENGTHENED,
    SOURCE_UPGRADED,
    SandboxPin,
    mode_strength,
)
from autoforge.state import load_state

from .conftest import FakeGitHub, make_engine, make_local_engine, write_feature
from .local_helpers import local_repo

AT = "2026-10-24T00:00:00+00:00"


def _cfg(mode: str) -> AutoForgeConfig:
    cfg = default_config()
    cfg.sandbox.mode = mode
    return cfg


def _pin(**over) -> dict:
    record = SandboxPin(mode=SANDBOX_REQUIRED, source=SOURCE_CREATED, recorded_at=AT).to_dict()
    record.update(over)
    return record


# -- the value ---------------------------------------------------------------------


def test_strength_is_what_a_launch_of_the_run_gets():
    """REMOTE's preferred never runs a launch unenforced (ADR 0005 D12.2)."""
    assert [mode_strength(m, local=True) for m in ("disabled", "preferred", "required")] == [
        0,
        1,
        2,
    ]
    assert [mode_strength(m, local=False) for m in ("disabled", "preferred", "required")] == [
        0,
        2,
        2,
    ]


@pytest.mark.parametrize(
    ("pinned", "mode", "local", "direction"),
    [
        (SANDBOX_REQUIRED, SANDBOX_DISABLED, True, -1),
        (SANDBOX_REQUIRED, SANDBOX_PREFERRED, True, -1),
        (SANDBOX_PREFERRED, SANDBOX_DISABLED, True, -1),
        (SANDBOX_PREFERRED, SANDBOX_REQUIRED, True, 1),
        (SANDBOX_DISABLED, SANDBOX_PREFERRED, True, 1),
        (SANDBOX_REQUIRED, SANDBOX_REQUIRED, True, 0),
        (SANDBOX_REQUIRED, SANDBOX_DISABLED, False, -1),
        (SANDBOX_PREFERRED, SANDBOX_DISABLED, False, -1),
        (SANDBOX_REQUIRED, SANDBOX_PREFERRED, False, 0),
        (SANDBOX_PREFERRED, SANDBOX_REQUIRED, False, 0),
    ],
)
def test_direction_compares_strength_not_names(pinned, mode, local, direction):
    pin = SandboxPin(mode=pinned, source=SOURCE_CREATED, recorded_at=AT)
    assert pin.direction(mode, local=local) == direction


def test_the_record_round_trips():
    pin = SandboxPin(mode=SANDBOX_PREFERRED, source=SOURCE_ACCEPTED, recorded_at=AT)
    assert pin.to_dict() == {
        "schema": PIN_SCHEMA,
        "mode": "preferred",
        "source": "accepted",
        "recorded_at": AT,
    }
    assert SandboxPin.from_dict(json.loads(json.dumps(pin.persisted()))) == pin
    assert pin.describe() == f"preferred (accepted {AT})"


@pytest.mark.parametrize(
    ("record", "needle"),
    [
        (_pin(schema=2), "schema 2 is not 1"),
        ({k: v for k, v in _pin().items() if k != "schema"}, "schema None is not 1"),
        ({**_pin(), "backend": "bwrap"}, "must hold exactly"),
        ({k: v for k, v in _pin().items() if k != "source"}, "must hold exactly"),
        ([], "must hold exactly"),
        (_pin(mode="off"), "mode must be one of disabled, preferred, required"),
        (_pin(mode=None), "mode must be one of"),
        (_pin(source="edited"), "source must be one of"),
        (_pin(recorded_at=""), "recorded_at must be a non-empty string"),
        (_pin(recorded_at="x" * 65), "recorded_at must be a non-empty string"),
        (_pin(recorded_at=7), "recorded_at must be a non-empty string"),
    ],
)
def test_a_malformed_record_is_refused(record, needle):
    with pytest.raises(StateError, match=needle):
        SandboxPin.from_dict(record)


def test_the_schema_is_pinned():
    """Bumping it is a deliberate change: a new schema's parser must still read this one."""
    assert PIN_SCHEMA == 1


# -- the engine: a REMOTE run ------------------------------------------------------


def _created(tmp_path, mode: str) -> ControllerEngine:
    """A REMOTE run created under ``mode``, its pin recorded and saved."""
    eng = make_engine(tmp_path / ".autoforge", cfg=_cfg(mode))
    eng.cli_global_flags = ("--state-dir", "sd")
    eng.record_run_configuration()
    eng.save()
    return eng


def _invocation(eng: ControllerEngine, mode: str) -> ControllerEngine:
    """A later command on ``eng``'s run under ``mode``, otherwise the same configuration."""
    provider = ScriptedProvider(None)
    later = ControllerEngine(
        config=_cfg(mode),
        state_dir=eng.paths.state_dir,
        workdir=eng.workdir,
        github=FakeGitHub(),
        providers=ProviderRegistry(overrides={"claude": provider, "opencode": provider}),
    )
    later.cli_global_flags = ()
    later.load()
    return later


def _events(eng: ControllerEngine) -> list[dict]:
    path = eng.paths.logs_dir / eng.state.run_id / "events.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_run_pins_the_mode_it_is_created_under(tmp_path):
    eng = _created(tmp_path, SANDBOX_REQUIRED)
    pin = SandboxPin.from_dict(load_state(eng.paths.state_file).sandbox_pin)
    assert (pin.mode, pin.source) == (SANDBOX_REQUIRED, SOURCE_CREATED)


@pytest.mark.parametrize("pinned", [SANDBOX_REQUIRED, SANDBOX_PREFERRED])
def test_a_remote_run_refuses_disabled_with_nothing_written(tmp_path, pinned):
    eng = _created(tmp_path, pinned)
    before = eng.paths.state_file.read_bytes()
    for dry_run in (False, True):
        later = _invocation(eng, SANDBOX_DISABLED)
        with pytest.raises(ConfigurationDriftError) as exc:
            later.check_run_configuration(("resume",), dry_run=dry_run)
        message = str(exc.value)
        assert f"is pinned to sandbox.mode {pinned} (created " in message
        assert "this command's sandbox.mode, disabled, contains its launches less" in message
        assert "'resume' refuses before any phase executes; nothing was written" in message
        assert f'\n  sandbox.mode: "{pinned}" -> "disabled"\n' in message
        assert f"run it with a configuration that sets sandbox.mode: {pinned}." in message
        assert "repeat it with --accept-config-change" in message
    assert eng.paths.state_file.read_bytes() == before
    assert _events(eng) == []


@pytest.mark.parametrize("pinned", [SANDBOX_REQUIRED, SANDBOX_PREFERRED])
def test_accepting_disabled_logs_it_then_pins_it(tmp_path, pinned):
    eng = _created(tmp_path, pinned)
    later = _invocation(eng, SANDBOX_DISABLED)
    notice = later.check_run_configuration(("step",), accept_change=True)
    assert notice.startswith("accepted the configuration change of run ")
    assert "the run is pinned to sandbox.mode disabled from now on:" in notice
    assert f'\n  sandbox.mode: "{pinned}" -> "disabled"' in notice
    saved = SandboxPin.from_dict(load_state(eng.paths.state_file).sandbox_pin)
    assert (saved.mode, saved.source) == (SANDBOX_DISABLED, SOURCE_ACCEPTED)
    [event] = _events(later)
    assert event["phase"] == "INITIALIZING-config-change"
    assert event["metadata"]["command"] == "step" and event["metadata"]["accepted"] is True
    assert event["metadata"]["changes"] == [
        {"key": "sandbox.mode", "recorded": f'"{pinned}"', "current": '"disabled"'}
    ]
    assert event["metadata"]["recorded_sandbox_pin"]["mode"] == pinned
    assert event["metadata"]["recorded_sandbox_pin"]["source"] == SOURCE_CREATED
    # From now on the accepted mode is the run's: disabled passes, and going
    # back up is a strengthening, not a change to accept.
    assert _invocation(eng, SANDBOX_DISABLED).check_run_configuration(("resume",)) == ""


def test_a_dry_run_says_what_it_would_accept_and_writes_nothing(tmp_path):
    eng = _created(tmp_path, SANDBOX_REQUIRED)
    before = eng.paths.state_file.read_bytes()
    later = _invocation(eng, SANDBOX_DISABLED)
    notice = later.check_run_configuration(("resume",), accept_change=True, dry_run=True)
    assert notice.startswith("[dry-run] would accept the configuration change of run ")
    assert '\n  sandbox.mode: "required" -> "disabled"' in notice
    assert eng.paths.state_file.read_bytes() == before
    assert _events(eng) == []


def test_a_model_and_a_mode_change_are_refused_and_accepted_together(tmp_path):
    eng = _created(tmp_path, SANDBOX_REQUIRED)
    cfg = _cfg(SANDBOX_DISABLED)
    cfg.profiles["fix"].model = "opus"
    later = _invocation(eng, SANDBOX_REQUIRED)
    later.config = cfg
    with pytest.raises(ConfigurationDriftError) as exc:
        later.check_run_configuration(("resume",))
    message = str(exc.value)
    assert '\n  profiles.fix.model: "fable" -> "opus"\n' in message
    assert '\n  sandbox.mode: "required" -> "disabled"\n' in message
    notice = later.check_run_configuration(("resume",), accept_change=True)
    assert "and the run is pinned to sandbox.mode disabled:" in notice
    [event] = _events(later)
    assert [change["key"] for change in event["metadata"]["changes"]] == [
        "profiles.fix.model",
        "sandbox.mode",
    ]


def test_a_stronger_mode_is_pinned_with_the_next_save(tmp_path):
    eng = _created(tmp_path, SANDBOX_DISABLED)
    later = _invocation(eng, SANDBOX_REQUIRED)
    notice = later.check_run_configuration(("resume",))
    assert "is pinned to the stronger sandbox.mode required (it was disabled)" in notice
    assert load_state(eng.paths.state_file).sandbox_pin["mode"] == SANDBOX_DISABLED
    later.save()
    saved = SandboxPin.from_dict(load_state(eng.paths.state_file).sandbox_pin)
    assert (saved.mode, saved.source) == (SANDBOX_REQUIRED, SOURCE_STRENGTHENED)
    assert _events(later) == []
    with pytest.raises(ConfigurationDriftError):
        _invocation(eng, SANDBOX_DISABLED).check_run_configuration(("resume",))


def test_a_remote_run_moves_between_required_and_preferred_freely(tmp_path):
    """REMOTE's preferred is required (ADR 0005 D12.2): no strength changes."""
    eng = _created(tmp_path, SANDBOX_REQUIRED)
    later = _invocation(eng, SANDBOX_PREFERRED)
    assert later.check_run_configuration(("resume",)) == ""
    later.save()
    assert load_state(eng.paths.state_file).sandbox_pin["mode"] == SANDBOX_REQUIRED


def test_a_run_without_a_pin_is_not_checked_and_pins_its_mode(tmp_path):
    """A state file written before #280 loads with no pin."""
    eng = _created(tmp_path, SANDBOX_REQUIRED)
    eng.state.sandbox_pin = {}
    eng.save()
    dry = _invocation(eng, SANDBOX_DISABLED).check_run_configuration(("resume",), dry_run=True)
    assert "[dry-run] run " in dry and "has no sandbox pin (its state file predates #280)" in dry
    assert "without --dry-run this command pins it to sandbox.mode disabled" in dry
    later = _invocation(eng, SANDBOX_DISABLED)
    notice = later.check_run_configuration(("resume",))
    assert "had no sandbox pin (its state file predates #280)" in notice
    assert "it is pinned to sandbox.mode disabled with its next save" in notice
    later.save()
    saved = SandboxPin.from_dict(load_state(eng.paths.state_file).sandbox_pin)
    assert (saved.mode, saved.source) == (SANDBOX_DISABLED, SOURCE_UPGRADED)


# -- the engine: a LOCAL run -------------------------------------------------------


def _local(tmp_path, mode: str):
    repo = local_repo(tmp_path)
    eng = make_local_engine(repo, write_feature(repo), cfg=_cfg(mode))
    eng.record_run_configuration()
    eng.save()
    return repo, eng


def _local_invocation(repo, eng: ControllerEngine, mode: str) -> ControllerEngine:
    later = make_local_engine(
        repo, None, cfg=_cfg(mode), state_dir=eng.paths.state_dir, start=False
    )
    later.load()
    return later


def test_a_local_run_pins_but_records_no_fingerprint(tmp_path):
    """A LOCAL run's contract defines its routing; the pin is the only record."""
    _, eng = _local(tmp_path, SANDBOX_PREFERRED)
    assert eng.state.config_fingerprint == {}
    pin = SandboxPin.from_dict(eng.state.sandbox_pin)
    assert (pin.mode, pin.source) == (SANDBOX_PREFERRED, SOURCE_CREATED)


@pytest.mark.parametrize(
    ("pinned", "mode"),
    [
        (SANDBOX_REQUIRED, SANDBOX_DISABLED),
        (SANDBOX_PREFERRED, SANDBOX_DISABLED),
        (SANDBOX_REQUIRED, SANDBOX_PREFERRED),
    ],
)
def test_a_local_run_refuses_a_weaker_mode_unless_accepted(tmp_path, pinned, mode):
    repo, eng = _local(tmp_path, pinned)
    before = eng.paths.state_file.read_bytes()
    with pytest.raises(ConfigurationDriftError) as exc:
        _local_invocation(repo, eng, mode).check_run_configuration(("step",))
    assert f'\n  sandbox.mode: "{pinned}" -> "{mode}"\n' in str(exc.value)
    assert "Changed (recorded -> this command)" not in str(exc.value)
    assert eng.paths.state_file.read_bytes() == before
    later = _local_invocation(repo, eng, mode)
    later.check_run_configuration(("step",), accept_change=True)
    saved = load_state(eng.paths.state_file)
    assert saved.sandbox_pin["mode"] == mode and saved.sandbox_pin["source"] == SOURCE_ACCEPTED
    assert saved.config_fingerprint == {}
    [event] = _events(later)
    assert event["metadata"]["recorded_sandbox_pin"]["mode"] == pinned
    assert "recorded_config_file" not in event["metadata"]
