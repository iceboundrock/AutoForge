"""The outer sandbox's modes and pre-launch gate in the engine (ADR 0005 D12.2, D12.4; #169).

The capability probe is the conftest's stand-in (:class:`tests.conftest.SandboxProbe`):
"supported" unless a test says which capability the host lacks. No launch
reaches a backend; the scripted provider records what the engine asked for.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from autoforge.cli import _render_outer_sandbox
from autoforge.config import default_config
from autoforge.errors import ConfigurationError, SandboxCapabilityError, SandboxLaunchRefusedError
from autoforge.sandbox_bwrap import Capability
from autoforge.state import load_state
from autoforge.transitions import Phase
from tests.conftest import implement, make_engine, make_local_engine
from tests.test_local import impl_result, local_repo, review_result, scripted, touch_impl


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


# -- LOCAL ---------------------------------------------------------------------
def _local(tmp_path, mode: str, validation=True):
    root = local_repo(tmp_path)
    cfg = _cfg(mode)
    if validation:
        cfg.local.validation_commands = [["python3", "-c", "pass"]]
    eng = make_local_engine(root, "features/add-filter.md", cfg=cfg)
    eng.provider._handler = scripted(
        eng,
        root,
        [
            (lambda r: touch_impl(r, "v1\n"), lambda e: impl_result()),
            (None, lambda e: review_result(e.state.workspace_fingerprint)),
        ],
    )
    return eng


def test_required_refuses_a_local_step_before_it_is_charged(tmp_path, sandbox_probe):
    eng = _local(tmp_path, "required")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    sandbox_probe.unsupported(Capability.LANDLOCK, "the kernel's Landlock ABI is 4")
    before = _snapshot(eng)
    with pytest.raises(SandboxCapabilityError) as caught:
        eng.step()
    message = str(caught.value)
    assert "Landlock" in message and "autoforge local doctor" in message
    assert "or preferred to fall back" in message
    assert _snapshot(eng) == before
    assert eng.provider.calls == []


def test_preferred_falls_back_visibly_in_local(tmp_path, sandbox_probe):
    eng = _local(tmp_path, "preferred")
    said: list[str] = []
    eng.progress_output = said.append
    sandbox_probe.unsupported(Capability.USER_NAMESPACE, "no user namespaces here")
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    (plan,) = [o.plan for o in eng.run(max_steps=1, dry_run=True)]
    (mode_note,) = [n for n in plan.notes if n.startswith("outer sandbox:")]
    assert "preferred" in mode_note and "logical isolation only" in mode_note
    assert "never called sandboxed" in mode_note
    assert any(n.startswith("outer sandbox policy, LOCAL agent") for n in plan.notes)
    assert any(n.startswith("outer sandbox policy, LOCAL validation command") for n in plan.notes)
    assert sandbox_probe.calls == 0

    eng.run(max_steps=5)
    assert eng.state.phase == Phase.DONE
    assert all(req.sandbox is None for req in eng.provider.calls)
    assert eng.provider.descriptions == []
    fallback = {
        "enforced": False,
        "mode": "preferred",
        "isolation": "logical isolation only",
        "fallback": "unsupported: this host cannot provide an unprivileged user namespace "
        "(no user namespaces here)",
    }
    executions = _executions(eng)
    assert {e["phase"] for e in executions} >= {"ANALYZE_EXECUTE", "REVIEW"}
    assert any("validation" in e["phase"] for e in executions)
    for execution in executions:
        assert execution["execution"]["sandbox"] == fallback, execution["phase"]
    # On stderr at each launch, never "sandboxed".
    notices = [line for line in said if "logical isolation only" in line]
    assert any("this agent" in line for line in notices)
    assert any("validation command" in line for line in notices)
    assert all("NOT inside the outer sandbox" in line for line in notices)
    assert sandbox_probe.calls == 1
    status = _render_outer_sandbox(eng.state, eng.config)[2]
    assert "fall" in status and "logical isolation only" in status


def test_preferred_on_a_capable_local_host_is_enforced(tmp_path, sandbox_probe):
    eng = _local(tmp_path, "preferred", validation=False)
    eng.run(max_steps=5)
    assert eng.state.phase == Phase.DONE
    assert all(req.sandbox is not None for req in eng.provider.calls)
    assert len(eng.provider.descriptions) == 2


def test_a_setup_failure_after_the_gate_never_falls_back(tmp_path, sandbox_probe):
    eng = _local(tmp_path, "preferred", validation=False)
    assert eng.step().next_phase == "ANALYZE_EXECUTE"
    launched = []

    def refuses(req):
        launched.append(req)
        raise SandboxLaunchRefusedError("bubblewrap failed to set the sandbox up: test")

    eng.provider.execute = refuses  # type: ignore[method-assign]
    with pytest.raises(SandboxLaunchRefusedError, match="set the sandbox up"):
        eng.step()
    # One launch, enforced; nothing re-ran without the sandbox.
    assert len(launched) == 1 and launched[0].sandbox is not None
