"""The outer sandbox's modes and pre-launch gate in the engine (ADR 0005 D12.2, D12.4; #169).

The capability probe is the conftest's stand-in (:class:`tests.conftest.SandboxProbe`):
"supported" unless a test says which capability the host lacks. No launch
reaches a backend; the scripted provider records what the engine asked for.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

import autoforge.engine as engine_module
from autoforge.cli import _render_outer_sandbox
from autoforge.config import MemoryConfig, ProfileConfig, default_config
from autoforge.errors import ConfigurationError, SandboxCapabilityError, SandboxLaunchRefusedError
from autoforge.sandbox_bwrap import Capability
from autoforge.state import load_state, save_state
from autoforge.transitions import Phase
from tests.conftest import implement, make_engine

# The real probe, taken before the conftest's stand-in replaces it for each test.
REAL_PROBE_SANDBOX = engine_module.probe_sandbox


def _cfg(mode: str):
    cfg = default_config()
    cfg.sandbox.mode = mode
    return cfg


def _snapshot(eng) -> dict[str, bytes]:
    """Every file under the state and log directories, by relative path and bytes."""
    found: dict[str, bytes] = {}
    for base in {eng.paths.state_dir, eng.paths.logs_dir}:
        base = Path(base)
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if path.is_file() and path.name != "lock":
                found[str(path.relative_to(base.parent))] = path.read_bytes()
    return found


def _executions(eng) -> list[dict]:
    run_dir = Path(eng.paths.logs_dir) / eng.state.run_id
    found = []
    for step in sorted(p for p in run_dir.iterdir() if (p / "execution.json").exists()):
        request = json.loads((step / "request.json").read_text(encoding="utf-8"))
        execution = json.loads((step / "execution.json").read_text(encoding="utf-8"))
        found.append({**request, "execution": execution})
    return found


# -- the gate refuses before anything (REMOTE) -----------------------------------
@pytest.mark.parametrize("mode", ["required", "preferred"])
def test_a_remote_step_on_an_incapable_host_is_refused_with_state_unchanged(
    tmp_state_dir, fake_github, sandbox_probe, mode
):
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=_cfg(mode), origin=True)
    # INITIALIZING launches nothing, so it is not gated.
    sandbox_probe.unsupported(Capability.PROCFS, "bwrap: Can't mount proc on /newroot/proc")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    assert sandbox_probe.calls == 0
    before = _snapshot(eng)
    with pytest.raises(SandboxCapabilityError) as caught:
        eng.step()
    assert isinstance(caught.value, ConfigurationError)
    assert caught.value.capability == "procfs"
    message = str(caught.value)
    assert "cannot provide a new procfs" in message and "Can't mount proc" in message
    assert "sandbox.mode: disabled" in message and "logical isolation only" in message
    assert "autoforge doctor" in message
    if mode == "preferred":
        assert "REMOTE run treats as required" in message
    # Byte-identical: no attempt saved, no step charged, no run-log step, no launch.
    assert _snapshot(eng) == before
    assert eng.provider.calls == []
    assert fake_github.effect_writes == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE


@pytest.mark.parametrize("mode", ["required", "preferred"])
def test_with_memory_on_the_refusal_never_offers_disabled_alone(
    tmp_state_dir, fake_github, sandbox_probe, mode
):
    """R7-F1 of PR #526: with Memory on, ``sandbox.mode: disabled`` is no way
    out of the refusal, since every Memory ANALYZE_EXECUTE launch is refused
    without the outer sandbox. The message says what doctor's mode row says:
    a capable host keeps Memory on, or Memory goes off before the run goes on
    unenforced. The state stays byte-identical, as without Memory."""
    cfg = _cfg(mode)
    cfg.memory = MemoryConfig(enabled=True)
    # The profile's half of Memory's proof, which the run's start checks.
    cfg.profiles["analyze_execute"] = ProfileConfig(
        "analyze_execute",
        "pi",
        "openai/fixture-model",
        "high",
        options={"context_files": "false", "require_oauth": "false"},
    )
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=cfg, origin=True)
    sandbox_probe.unsupported(Capability.PROCFS, "bwrap: Can't mount proc on /newroot/proc")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    before = _snapshot(eng)
    with pytest.raises(SandboxCapabilityError) as caught:
        eng.step()
    assert caught.value.capability == "procfs"
    message = str(caught.value)
    assert "cannot provide a new procfs" in message and "autoforge doctor" in message
    assert "memory.enabled is true" in message and "refused without the outer sandbox" in message
    assert "make the host capable to keep Memory on" in message
    assert "turn Memory off and set sandbox.mode: disabled" in message
    assert "logical isolation only" in message
    # `disabled` is never offered on its own.
    assert "or set sandbox.mode: disabled" not in message
    if mode == "preferred":
        assert "REMOTE run treats as required" in message
    assert _snapshot(eng) == before
    assert eng.provider.calls == []
    assert fake_github.effect_writes == []
    # Memory recorded for the run, the configuration since turned off: the
    # launch would still be a Memory launch, as the launch decides it, so
    # the refusal says the same.
    run = load_state(eng.paths.state_file)
    run.issue_memory = run.memory_setting(True)
    save_state(run, eng.paths.state_file)
    eng.load()
    eng.config.memory = MemoryConfig(enabled=False)
    before = _snapshot(eng)
    with pytest.raises(SandboxCapabilityError) as caught:
        eng.step()
    assert "turn Memory off and set sandbox.mode: disabled" in str(caught.value)
    assert _snapshot(eng) == before


@pytest.mark.parametrize("mode", ["required", "preferred"])
def test_with_memory_off_the_refusal_still_offers_disabled(
    tmp_state_dir, fake_github, sandbox_probe, mode
):
    """R7-F1 of PR #526: the Memory-off guidance is unchanged: make the host
    capable, or set ``sandbox.mode: disabled`` for logical isolation only;
    nothing of Memory is mentioned."""
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=_cfg(mode), origin=True)
    sandbox_probe.unsupported(Capability.PROCFS, "bwrap: Can't mount proc on /newroot/proc")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    with pytest.raises(SandboxCapabilityError) as caught:
        eng.step()
    message = str(caught.value)
    assert "make the host capable, or set sandbox.mode: disabled to run with" in message
    assert "logical isolation only" in message
    assert "Memory" not in message and "memory" not in message


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="the backend is Linux-only")
@pytest.mark.parametrize("mode", ["required", "preferred"])
def test_a_python_without_memfd_create_is_refused_by_the_real_probe_with_state_unchanged(
    tmp_state_dir, fake_github, monkeypatch, mode
):
    """PR #425 R1-F2 (#389): on a Python built without ``os.memfd_create``
    (uv's CPython 3.12.13 build), the real probe, not a stand-in, refuses
    the step before anything of it is saved or launched, naming the
    interpreter as what the host cannot provide, where it used to raise
    ``AttributeError`` from inside the launch. Holds on every interpreter:
    the call is removed for the test."""
    monkeypatch.setattr(engine_module, "probe_sandbox", REAL_PROBE_SANDBOX)
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=_cfg(mode), origin=True)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    monkeypatch.delattr(os, "memfd_create", raising=False)
    before = _snapshot(eng)
    with pytest.raises(SandboxCapabilityError) as caught:
        eng.step()
    assert caught.value.capability == "interpreter"
    message = str(caught.value)
    assert "cannot provide a Python with os.memfd_create" in message
    assert "has no os.memfd_create" in message and "sandbox.mode: disabled" in message
    assert _snapshot(eng) == before
    assert eng.provider.calls == []
    assert fake_github.effect_writes == []
    assert load_state(eng.paths.state_file).phase is Phase.ANALYZE_EXECUTE


def test_the_probe_is_cached_for_the_engine_never_across_engines(
    tmp_state_dir, fake_github, sandbox_probe
):
    eng = make_engine(
        tmp_state_dir, implement, github=fake_github, cfg=_cfg("required"), origin=True
    )
    eng.step()
    sandbox_probe.unsupported()
    for _ in range(2):
        with pytest.raises(SandboxCapabilityError):
            eng.step()
    assert sandbox_probe.calls == 1
    # A new controller process probes again: a host made capable is seen.
    again = make_engine(
        tmp_state_dir.parent / "other" / ".autoforge", implement, cfg=_cfg("required")
    )
    again.step()
    with pytest.raises(SandboxCapabilityError):
        again.step()
    assert sandbox_probe.calls == 2


def test_a_capable_host_launches_inside(tmp_state_dir, fake_github, sandbox_probe):
    eng = make_engine(
        tmp_state_dir, implement, github=fake_github, cfg=_cfg("preferred"), origin=True
    )
    eng.step()
    assert eng.step().next_phase == "REVIEW"
    (req,) = eng.provider.calls
    assert req.sandbox is not None and len(eng.provider.descriptions) == 1
    assert sandbox_probe.calls == 1


def test_disabled_never_probes_and_says_logical_isolation_only(
    tmp_state_dir, fake_github, sandbox_probe
):
    eng = make_engine(tmp_state_dir, implement, github=fake_github, origin=True)
    sandbox_probe.unsupported()
    eng.step()
    (plan,) = [o.plan for o in eng.run(max_steps=1, dry_run=True)]
    (note,) = [n for n in plan.notes if n.startswith("outer sandbox")]
    assert "logical isolation only" in note and "a risk" in note
    assert eng.step().next_phase == "REVIEW"
    (record,) = [e for e in _executions(eng) if e["phase"] == "ANALYZE_EXECUTE"]
    assert record["execution"].get("sandbox", {}) == {}
    assert sandbox_probe.calls == 0


def test_an_operator_path_over_a_denied_root_is_refused_before_anything(
    tmp_state_dir, fake_github, sandbox_probe
):
    cfg = _cfg("required")
    eng = make_engine(tmp_state_dir, implement, github=fake_github, cfg=cfg, origin=True)
    eng.step()
    cfg.sandbox.read_only_paths = [str(tmp_state_dir)]
    before = _snapshot(eng)
    with pytest.raises(ConfigurationError, match=r"sandbox\.read_only_paths\[0\].*overlaps"):
        eng.step()
    assert _snapshot(eng) == before and eng.provider.calls == []
    assert sandbox_probe.calls == 0


# -- the dry run and status ------------------------------------------------------
def test_a_dry_run_shows_the_policy_by_class_and_never_probes(
    tmp_state_dir, fake_github, sandbox_probe
):
    eng = make_engine(tmp_state_dir, None, github=fake_github, cfg=_cfg("required"))
    eng.step()
    eng.config.sandbox.read_only_paths = ["/opt/toolchain"]
    (plan,) = [o.plan for o in eng.run(max_steps=1, dry_run=True)]
    notes = [n for n in plan.notes if n.startswith("outer sandbox")]
    assert notes[0].startswith("outer sandbox: required (sandbox.mode)")
    remote, export = notes[1:]
    assert remote.startswith("outer sandbox policy, REMOTE agent")
    assert "writable: the issue worktree" in remote and "the shared object store" in remote
    assert "/opt/toolchain (sandbox.read_only_paths)" in remote
    assert export.startswith("outer sandbox policy, export command")
    assert "network host" in export or "network" in export
    assert sandbox_probe.calls == 0


def test_status_names_the_mode_without_probing(tmp_state_dir, fake_github, sandbox_probe):
    eng = make_engine(tmp_state_dir, None, github=fake_github, cfg=_cfg("preferred"))
    lines = _render_outer_sandbox(eng.state, eng.config)
    assert lines[1] == "Outer sandbox:"
    assert "REMOTE run treats as required" in lines[2]
    eng.config.sandbox.mode = "disabled"
    assert "logical isolation only" in _render_outer_sandbox(eng.state, eng.config)[2]
    assert sandbox_probe.calls == 0


# -- preferred is required (ADR 0009 D7) -------------------------------------------
def test_a_setup_failure_after_the_gate_never_runs_unenforced(
    tmp_state_dir, fake_github, sandbox_probe
):
    """``preferred`` has no fallback: a launch the backend refuses after the gate
    is refused, once, enforced, and nothing re-runs without the sandbox."""
    eng = make_engine(
        tmp_state_dir, implement, github=fake_github, cfg=_cfg("preferred"), origin=True
    )
    said: list[str] = []
    eng.progress_output = said.append
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    launched = []

    def refuses(req):
        launched.append(req)
        raise SandboxLaunchRefusedError("bubblewrap failed to set the sandbox up: test")

    eng.provider.execute = refuses  # type: ignore[method-assign]
    with pytest.raises(SandboxLaunchRefusedError, match="set the sandbox up"):
        eng.step()
    assert len(launched) == 1 and launched[0].sandbox is not None
    assert not [line for line in said if "logical isolation only" in line]
    for execution in _executions(eng):
        assert execution["execution"].get("sandbox", {}).get("fallback") is None
