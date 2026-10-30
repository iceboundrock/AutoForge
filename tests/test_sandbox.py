"""The outer sandbox's description, compiler and backend wiring (ADR 0005, #167).

Three layers, none of which needs a working Bubblewrap:

- the policy compiler (:func:`autoforge.sandbox.compile_launch`) on paths
  under ``tmp_path``: every refusal names the path and the rule and happens
  before anything is spawned, and what it accepts is ordered parents first;
- the backend's options (``sandbox_bwrap._options``): the flags every launch
  gets, the ones it never gets, and where the environment goes;
- the executor and duplex wiring, against a fake ``bwrap`` on ``PATH`` that
  reads its options from the descriptor it is given, runs the command and
  writes ``bwrap``'s status records (or fails its setup on request), so the
  launch, the refusal on a setup failure and the facts are tested on any
  Linux host. The launch's process scope (keyring, Landlock) is the real
  backend's and is tested in ``tests/test_sandbox_bwrap.py``.
"""

from __future__ import annotations

import ast
import ctypes
import errno
import json
import os
import socket
import stat
import subprocess
import sys
import types
from pathlib import Path

import pytest

from autoforge import executor, sandbox, sandbox_bwrap
from autoforge.errors import (
    ExecutionError,
    SandboxLaunchRefusedError,
    SandboxPolicyError,
)
from autoforge.executor import ExecutionRequest, describe_leftovers, execute
from autoforge.executor_duplex import DuplexRequest, Eof, Record, start_duplex
from autoforge.sandbox import (
    TEARDOWN_UNCOUNTED,
    Access,
    Bind,
    LaunchDescription,
    Link,
    Mask,
    NetworkPosition,
    Origin,
    Overlay,
    OverlayRequest,
    PrivateTmpfs,
    ProjectionRequest,
    SandboxFacts,
    SandboxRequest,
    SysPolicy,
    compile_launch,
)
from tests.conftest import bind_unix

SRC = Path(__file__).resolve().parents[1] / "src" / "autoforge"
RO = Access.READ_ONLY
RW = Access.WRITABLE
ENGINE = Origin.ENGINE
DECLARED = Origin.DECLARED

_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="memfd, /proc and the backend are Linux-only"
)


# ------------------------------------------------------------------ helpers


def _dir(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _file(path: Path, text: str = "x\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def _base(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


def _request(tmp_path: Path, **kw: object) -> SandboxRequest:
    base = _base(tmp_path)
    kw.setdefault("home", _dir(base / "home"))
    kw.setdefault("controller_env", {})
    kw.setdefault("system_runtime", False)
    return SandboxRequest(**kw)  # type: ignore[arg-type]


def P(source: str, access: Access = RO, origin: Origin = ENGINE, **kw: object) -> ProjectionRequest:
    return ProjectionRequest(source, access, origin, **kw)  # type: ignore[arg-type]


def _targets(description: LaunchDescription) -> list[str]:
    return [mount.target for mount in description.mounts]


@pytest.fixture
def no_spawn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail the test if anything is spawned while it runs."""

    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("the compiler spawned a process")

    monkeypatch.setattr(subprocess, "Popen", refuse)
    monkeypatch.setattr(os, "fork", refuse)
    monkeypatch.setattr(os, "posix_spawn", refuse, raising=False)


# ---------------------------------------------------------------- refusals
#
# Each case builds its scenario under the test's base directory and returns
# the request, the path the refusal must name and the rule.


def _symlinked_parent(b: Path) -> tuple[SandboxRequest, str, str]:
    _dir(b / "real" / "ws")
    (b / "link").symlink_to(b / "real")
    source = str(b / "link" / "ws")
    return (
        _request(b, projections=(P(source, RW),)),
        source,
        "C2 no link in a controller-owned path",
    )


def _symlinked_source(b: Path) -> tuple[SandboxRequest, str, str]:
    (b / "wslink").symlink_to(_dir(b / "ws"))
    source = str(b / "wslink")
    return (
        _request(b, projections=(P(source, RW),)),
        source,
        "C2 no link in a controller-owned path",
    )


def _socket(b: Path) -> tuple[SandboxRequest, str, str]:
    path = str(b / "agent.sock")
    sock = socket.socket(socket.AF_UNIX)
    bind_unix(sock, b / "agent.sock")
    sock.close()
    return _request(b, projections=(P(path, RO, DECLARED),)), path, "C3 kinds"


def test_a_socket_is_bound_at_a_path_longer_than_sun_path(tmp_path: Path) -> None:
    """``sun_path`` holds 107 bytes; under pytest-xdist or a long ``TMPDIR`` a
    socket under ``tmp_path`` can be longer (#340). ``conftest.bind_unix``
    binds it there anyway, and leaves the working directory as it was."""
    path = _dir(tmp_path / ("d" * 120)) + "/agent.sock"
    cwd = os.getcwd()
    sock = socket.socket(socket.AF_UNIX)
    try:
        with pytest.raises(OSError, match="too long"):
            sock.bind(path)
        bind_unix(sock, Path(path))
        assert stat.S_ISSOCK(os.lstat(path).st_mode)
    finally:
        sock.close()
    assert os.getcwd() == cwd


def _fifo(b: Path) -> tuple[SandboxRequest, str, str]:
    path = str(b / "fifo")
    os.mkfifo(path)
    return _request(b, projections=(P(path, RO, DECLARED),)), path, "C3 kinds"


def _ssh_link(b: Path) -> tuple[SandboxRequest, str, str]:
    ssh = _dir(b / "home" / ".ssh")
    (b / "innocent").symlink_to(ssh)
    return (
        _request(b, projections=(P(str(b / "innocent"), RO, DECLARED),)),
        ssh,
        "D4.3 forbidden source",
    )


def _interpreter(b: Path) -> str:
    """A real interpreter under ``b/runtime``, the root a provider projects."""
    return _file(b / "runtime" / "bin" / "python3.14", "#!/bin/sh\n")


def _runtime(b: Path) -> ProjectionRequest:
    return P(str(b / "runtime"), RO, DECLARED)


def _spelled_through(
    b: Path, holder: Path, rule: str, **kw: object
) -> tuple[SandboxRequest, str, str]:
    """A spelled path that is a link to the interpreter in ``holder``,
    refused under ``rule``, naming that link."""
    holder.mkdir(parents=True, exist_ok=True)
    (holder / "python3").symlink_to(_interpreter(b))
    link = str(holder / "python3")
    kw.setdefault("projections", (_runtime(b),))
    return _request(b, spelled_paths=(link,), **kw), link, rule


def _spelled_leaving(
    b: Path, left: Path, rule: str, **kw: object
) -> tuple[SandboxRequest, str, str]:
    """A spelled path that is a link passing through the directory ``left``
    and out of it by ``..`` to the interpreter, refused under ``rule``,
    naming ``left``."""
    interpreter = _interpreter(b)
    _dir(left)
    link = b / "alias" / "python3"
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(f"{left}/../{os.path.relpath(interpreter, left.parent)}")
    kw.setdefault("projections", (_runtime(b),))
    return _request(b, spelled_paths=(str(link),), **kw), str(left), rule


def _home_child(rel: str, file: bool = False):
    def build(b: Path) -> tuple[SandboxRequest, str, str]:
        path = b / "home" / rel
        source = _file(path) if file else _dir(path)
        return _request(b, projections=(P(source, RO, DECLARED),)), source, "D4.3 forbidden source"

    return build


CASES = {
    "relative engine source": lambda b: (
        _request(b, projections=(P("ws"),)),
        "ws",
        "C1 resolved source",
    ),
    "relative declared source": lambda b: (
        _request(b, projections=(P("ws", RO, DECLARED),)),
        "ws",
        "C1 resolved source",
    ),
    "missing declared source": lambda b: (
        _request(b, projections=(P(str(b / "missing"), RO, DECLARED),)),
        str(b / "missing"),
        "C1 resolved source",
    ),
    "missing engine source": lambda b: (
        _request(b, projections=(P(str(b / "missing"), RW),)),
        str(b / "missing"),
        "C1 resolved source",
    ),
    "relative denied root": lambda b: (
        _request(b, denied_roots=("state",)),
        "state",
        "C1 resolved source",
    ),
    "engine source through a symlinked parent": _symlinked_parent,
    "engine source that is a symlink": _symlinked_source,
    "a socket": _socket,
    "a FIFO": _fifo,
    "source inside a denied root": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "state" / "x"), RW),),
            denied_roots=(str(b / "state"),),
        ),
        str(b / "state" / "x"),
        "C4 denied roots",
    ),
    "denied root inside a read-only projection, unmasked": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "ws")),),
            denied_roots=(_dir(b / "ws" / ".autoforge"),),
        ),
        str(b / "ws" / ".autoforge"),
        "C4 denied roots",
    ),
    "denied root deep inside a writable projection": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "ws"), RW),),
            denied_roots=(_dir(b / "ws" / "a" / "state"),),
        ),
        str(b / "ws" / "a" / "state"),
        "C4 denied roots",
    ),
    "denied file inside a writable projection": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "ws"), RW),),
            denied_roots=(_file(b / "ws" / "state.json"),),
        ),
        str(b / "ws" / "state.json"),
        "C4 denied roots",
    ),
    "mask inside no projection": lambda b: (
        _request(b, masks=(_dir(b / "loose"),)),
        str(b / "loose"),
        "C4 denied roots",
    ),
    "deep mask inside a writable projection": lambda b: (
        _request(b, projections=(P(_dir(b / "ws"), RW),), masks=(_dir(b / "ws" / "a" / "b"),)),
        str(b / "ws" / "a" / "b"),
        "C4 denied roots",
    ),
    "declared carve-out": lambda b: (
        _request(b, projections=(P(_dir(b / "d"), RO, DECLARED, carve_out=True),)),
        str(b / "d"),
        "C5 carve-outs",
    ),
    "declared projection inside a mask": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "ws")), P(_dir(b / "ws" / "m" / "x"), RO, DECLARED)),
            masks=(str(b / "ws" / "m"),),
        ),
        str(b / "ws" / "m" / "x"),
        "C5 carve-outs",
    ),
    "engine projection inside a mask, not a carve-out": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "ws")), P(_dir(b / "ws" / "m" / "x"), RW)),
            masks=(str(b / "ws" / "m"),),
        ),
        str(b / "ws" / "m" / "x"),
        "C5 carve-outs",
    ),
    "overlapping declared projections": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "a"), RO, DECLARED), P(_dir(b / "a" / "b"), RO, DECLARED)),
        ),
        str(b / "a"),
        "C6 overlaps",
    ),
    "the same declared projection twice": lambda b: (
        _request(b, projections=(P(_dir(b / "a"), RO, DECLARED), P(str(b / "a"), RW, DECLARED))),
        str(b / "a"),
        "C6 overlaps",
    ),
    "declared writable inside a read-only engine projection": lambda b: (
        _request(b, projections=(P(_dir(b / "ws")), P(_dir(b / "ws" / "out"), RW, DECLARED))),
        str(b / "ws" / "out"),
        "C6 overlaps",
    ),
    "declared projection containing an engine one": lambda b: (
        _request(b, projections=(P(_dir(b / "d" / "e")), P(str(b / "d"), RO, DECLARED))),
        str(b / "d"),
        "C6 overlaps",
    ),
    "the same engine projection twice": lambda b: (
        _request(b, projections=(P(_dir(b / "ws")), P(str(b / "ws"), RW))),
        str(b / "ws"),
        "C6 overlaps",
    ),
    "overlay inside a writable projection": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "ws"), RW),),
            overlays=(OverlayRequest(_dir(b / "ws" / "o"), _dir(b / "up"), _dir(b / "work")),),
        ),
        str(b / "ws" / "o"),
        "C6 overlaps",
    ),
    "overlay layers that overlap": lambda b: (
        _request(
            b,
            overlays=(OverlayRequest(_dir(b / "low"), _dir(b / "low" / "up"), _dir(b / "work")),),
        ),
        str(b / "low" / "up"),
        "C6 overlaps",
    ),
    "overlay layer visible inside": lambda b: (
        _request(
            b,
            projections=(P(_dir(b / "layers"), RW),),
            overlays=(
                OverlayRequest(_dir(b / "low"), _dir(b / "layers" / "up"), _dir(b / "work")),
            ),
        ),
        str(b / "layers" / "up"),
        "C6 overlaps",
    ),
    "relocation outside the private home": lambda b: (
        _request(b, projections=(P(_dir(b / "d"), RO, DECLARED, target="/opt/tool"),)),
        "/opt/tool",
        "C7 relocations",
    ),
    "relocation to an unclean target": lambda b: (
        _request(b, projections=(P(_dir(b / "d"), RO, DECLARED, target="rel/tool"),)),
        "rel/tool",
        "C7 relocations",
    ),
    "the operator's home": lambda b: (
        _request(b, projections=(P(_dir(b / "home"), RO, DECLARED),)),
        str(b / "home"),
        "D4.3 forbidden source",
    ),
    "an ancestor of the operator's home": lambda b: (
        _request(b, projections=(P(str(b), RO, DECLARED),)),
        str(b),
        "D4.3 forbidden source",
    ),
    "~/.ssh": _home_child(".ssh"),
    "a key in ~/.ssh": _home_child(".ssh/id_ed25519", file=True),
    "the gh hosts file": _home_child(".config/gh/hosts.yml", file=True),
    "a directory holding the gh configuration": _home_child(".config"),
    "the git credential store": _home_child(".git-credentials", file=True),
    "~/.gitconfig": _home_child(".gitconfig", file=True),
    "~/.netrc": _home_child(".netrc", file=True),
    "~/.gnupg": _home_child(".gnupg"),
    "a keyring file": _home_child(".local/share/keyrings/login.keyring", file=True),
    "the password store": _home_child(".password-store"),
    "a link to ~/.ssh": _ssh_link,
    "$GH_CONFIG_DIR": lambda b: (
        _request(
            b,
            controller_env={"GH_CONFIG_DIR": _dir(b / "ghcfg")},
            projections=(P(_file(b / "ghcfg" / "hosts.yml"), RO, DECLARED),),
        ),
        str(b / "ghcfg" / "hosts.yml"),
        "D4.3 forbidden source",
    ),
    "$XDG_CONFIG_HOME/gh": lambda b: (
        _request(
            b,
            controller_env={"XDG_CONFIG_HOME": _dir(b / "xdg")},
            projections=(P(_dir(b / "xdg" / "gh"), RO, DECLARED),),
        ),
        str(b / "xdg" / "gh"),
        "D4.3 forbidden source",
    ),
    "$XDG_RUNTIME_DIR": lambda b: (
        _request(
            b,
            controller_env={"XDG_RUNTIME_DIR": _dir(b / "runtime")},
            projections=(P(_file(b / "runtime" / "bus"), RO, DECLARED),),
        ),
        str(b / "runtime" / "bus"),
        "D4.3 forbidden source",
    ),
    "a writable declared file": lambda b: (
        _request(b, projections=(P(_file(b / "f"), RW, DECLARED),)),
        str(b / "f"),
        "D7.1 no writable file",
    ),
    "a keyring login": lambda b: (
        _request(b, keyring_items=("login-keyring-item",)),
        "login-keyring-item",
        "D7.2 keyring",
    ),
    "a writable operator configuration root": lambda b: (
        _request(
            b,
            operator_config_roots=(_dir(b / "opcfg"),),
            projections=(P(str(b / "opcfg"), RW, DECLARED),),
        ),
        str(b / "opcfg"),
        "D7.2 operator configuration root",
    ),
    "a writable directory inside an operator root": lambda b: (
        _request(
            b,
            operator_config_roots=(str(b / "opcfg"),),
            projections=(P(_dir(b / "opcfg" / "sub"), RW, DECLARED),),
        ),
        str(b / "opcfg" / "sub"),
        "D7.2 operator configuration root",
    ),
    "a relative home": lambda b: (_request(b, home="home"), "home", "D8.1 private home"),
    "the root as the home": lambda b: (_request(b, home="/"), "/", "D8.1 private home"),
    "a zero tmpfs bound": lambda b: (_request(b, tmp_size_bytes=0), "0", "D8.1 private home"),
    "an owned environment name": lambda b: (
        _request(b, env=(("HOME", "/elsewhere"),)),
        "HOME",
        "D8.2 environment",
    ),
    "an invalid environment name": lambda b: (
        _request(b, env=(("A-B", "1"),)),
        "A-B",
        "D8.2 environment",
    ),
    "a name set twice": lambda b: (
        _request(b, env=(("TOOL_DIR", "/a"), ("TOOL_DIR", "/b"))),
        "TOOL_DIR",
        "D8.2 environment",
    ),
    "a name both set and unset": lambda b: (
        _request(b, env=(("TOOL_DIR", "/a"),), env_unset=("TOOL_DIR",)),
        "TOOL_DIR",
        "D8.2 environment",
    ),
    "an owned name unset": lambda b: (
        _request(b, env_unset=("HOME",)),
        "HOME",
        "D8.2 environment",
    ),
    "a relative spelled path": lambda b: (
        _request(b, spelled_paths=("bin/python3",)),
        "bin/python3",
        "C1 resolved source",
    ),
    "a missing spelled path": lambda b: (
        _request(b, spelled_paths=(str(b / "missing" / "python3"),)),
        str(b / "missing" / "python3"),
        "C1 resolved source",
    ),
    "a spelled path through a link in a mask": lambda b: _spelled_through(
        b,
        b / "ws" / "m",
        "D13.1 spelled path",
        projections=(P(_dir(b / "ws")), _runtime(b)),
        masks=(str(b / "ws" / "m"),),
    ),
    "a spelled path through a link in a denied root": lambda b: _spelled_through(
        b, b / "state", "C4 denied roots", denied_roots=(str(b / "state"),)
    ),
    "a spelled path through a link in ~/.ssh": lambda b: _spelled_through(
        b, b / "home" / ".ssh", "D4.3 forbidden source"
    ),
    "a spelled path to an interpreter nothing shows": lambda b: _spelled_through(
        b, b / "alias", "D13.1 spelled path", projections=()
    ),
    "a spelled path leaving a directory in a denied root": lambda b: _spelled_leaving(
        b, b / "state", "C4 denied roots", denied_roots=(str(b / "state"),)
    ),
    "a spelled path leaving ~/.ssh": lambda b: _spelled_leaving(
        b, b / "home" / ".ssh", "D4.3 forbidden source"
    ),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_every_refusal_names_the_path_and_the_rule_before_any_spawn(
    tmp_path: Path, case: str, no_spawn: None
) -> None:
    request, path, rule = CASES[case](_base(tmp_path))
    with pytest.raises(SandboxPolicyError) as info:
        compile_launch(request)
    err = info.value
    assert (err.path, err.rule) == (path, rule)
    assert str(err).startswith(f"sandbox policy refuses {path}: {rule}: ")


def test_refusal_never_quotes_a_file(tmp_path: Path) -> None:
    b = _base(tmp_path)
    secret = "contents-that-must-not-appear"
    source = _file(b / "home" / ".git-credentials", secret)
    with pytest.raises(SandboxPolicyError) as info:
        compile_launch(_request(b, projections=(P(source, RO, DECLARED),)))
    assert secret not in str(info.value)


@pytest.mark.skipif(not os.path.isdir("/run"), reason="no /run on this host")
def test_run_is_a_forbidden_source(tmp_path: Path) -> None:
    with pytest.raises(SandboxPolicyError) as info:
        compile_launch(_request(tmp_path, projections=(P("/run", RO, DECLARED),)))
    assert (info.value.path, info.value.rule) == ("/run", "D4.3 forbidden source")


@pytest.mark.skipif(not os.path.exists("/dev/null"), reason="no /dev/null")
def test_a_device_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxPolicyError) as info:
        compile_launch(_request(tmp_path, projections=(P("/dev/null", RO, DECLARED),)))
    assert (info.value.path, info.value.rule) == ("/dev/null", "C3 kinds")
    assert "character device" in str(info.value)


def test_the_private_temp_cannot_be_projected_over(tmp_path: Path) -> None:
    # A home outside /tmp, so /tmp itself is not refused as the home's ancestor.
    request = SandboxRequest(
        home="/nonexistent-autoforge-home",
        controller_env={},
        system_runtime=False,
        projections=(P(os.path.realpath("/tmp"), RW),),
    )
    with pytest.raises(SandboxPolicyError) as info:
        compile_launch(request)
    assert info.value.rule == "C6 overlaps"


# ------------------------------------------------------------- what compiles


def _parents_first(description: LaunchDescription) -> None:
    targets = _targets(description)
    for i, earlier in enumerate(targets):
        for later in targets[i + 1 :]:
            assert not earlier.startswith(later.rstrip("/") + "/"), (earlier, later)


def test_private_home_and_temp_and_the_environment(tmp_path: Path) -> None:
    b = _base(tmp_path)
    description = compile_launch(
        _request(
            b,
            home_size_bytes=4096 * 1024,
            env=(("TOOL_DIR", "/opt/tool"),),
            env_unset=("SSH_AUTH_SOCK",),
        )
    )
    home = str(b / "home")
    assert PrivateTmpfs(home, 0o700, 4096 * 1024) in description.mounts
    assert PrivateTmpfs("/tmp", 0o1777, None) in description.mounts
    assert dict(description.env_set) == {
        "HOME": home,
        "XDG_CONFIG_HOME": f"{home}/.config",
        "XDG_DATA_HOME": f"{home}/.local/share",
        "XDG_STATE_HOME": f"{home}/.local/state",
        "XDG_CACHE_HOME": f"{home}/.cache",
        "TMPDIR": "/tmp",
        "TOOL_DIR": "/opt/tool",
    }
    assert description.env_unset == ("SSH_AUTH_SOCK", "XDG_RUNTIME_DIR")
    assert dict(description.tmpfs_bounds()) == {"/tmp": None, home: 4096 * 1024}
    assert description.network is NetworkPosition.HOST
    assert description.sys is SysPolicy.READ_ONLY
    _parents_first(description)


def test_projections_under_the_private_temp_follow_it(tmp_path: Path) -> None:
    b = _base(tmp_path)
    ws = _dir(b / "ws")
    description = compile_launch(_request(b, projections=(P(ws, RW),)))
    targets = _targets(description)
    if ws.startswith("/tmp/"):
        assert targets.index("/tmp") < targets.index(ws)
    _parents_first(description)


def test_a_mask_under_a_read_only_projection_with_a_writable_carve_out(tmp_path: Path) -> None:
    b = _base(tmp_path)
    common = _dir(b / "repo" / ".git")
    carve = _dir(b / "repo" / ".git" / "autoforge" / "worktrees" / "7")
    description = compile_launch(
        _request(
            b,
            projections=(P(common), P(carve, RW, carve_out=True)),
            masks=(f"{common}/autoforge",),
            denied_roots=(f"{common}/autoforge",),
        )
    )
    mounts = list(description.mounts)
    bind = Bind(common, common, RO, True)
    mask = Mask(f"{common}/autoforge")
    carved = Bind(carve, carve, RW, True)
    assert mounts.index(bind) < mounts.index(mask) < mounts.index(carved)


def test_a_denied_root_that_is_a_direct_child_of_a_writable_projection_is_masked(
    tmp_path: Path,
) -> None:
    b = _base(tmp_path)
    ws = _dir(b / "ws")
    state = _dir(b / "ws" / ".autoforge")
    description = compile_launch(
        _request(b, projections=(P(ws, RW),), denied_roots=(state, str(b / "ws" / "absent")))
    )
    assert Mask(state) in description.mounts
    assert Mask(str(b / "ws" / "absent")) not in description.mounts


def test_an_absent_mask_is_skipped(tmp_path: Path) -> None:
    b = _base(tmp_path)
    ws = _dir(b / "ws")
    description = compile_launch(
        _request(b, projections=(P(ws),), masks=(str(b / "ws" / "absent"),))
    )
    assert not any(isinstance(mount, Mask) for mount in description.mounts)


def test_a_declared_read_only_projection_already_visible_is_covered(tmp_path: Path) -> None:
    b = _base(tmp_path)
    ws = _dir(b / "ws")
    inner = _dir(b / "ws" / "docs")
    description = compile_launch(_request(b, projections=(P(ws), P(inner, RO, DECLARED))))
    assert description.covered == (inner,)
    assert inner not in _targets(description)


def test_a_declared_projection_through_a_link_is_resolved(tmp_path: Path) -> None:
    b = _base(tmp_path)
    real = _dir(b / "real" / "tool")
    (b / "via").symlink_to(b / "real")
    description = compile_launch(
        _request(b, projections=(P(str(b / "via" / "tool"), RO, DECLARED),))
    )
    assert Bind(real, real, RO, True) in description.mounts


def _brew(prefix: Path, interpreter: str) -> str:
    """A Homebrew-shaped prefix: ``opt/python`` links to its ``Cellar``
    keg, whose ``bin/python3`` links to ``interpreter``; a file beside every
    link. The spelled path through both links."""
    keg = prefix / "Cellar" / "python" / "3.14"
    _file(keg / "bin" / "pip")
    _file(prefix / "opt" / "openssl" / "lib" / "libssl.so")
    (keg / "bin" / "python3").symlink_to(interpreter)
    (prefix / "opt" / "python").symlink_to("../Cellar/python/3.14")
    return str(prefix / "opt" / "python" / "bin" / "python3")


@pytest.mark.parametrize("where", ["empty root", "private temp", "private home"])
def test_a_spelled_path_gets_each_link_on_its_way_and_nothing_beside_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """#225: an absolute ``#!`` through links in directories nothing
    projects. Each link is recreated where it is (the empty root, the
    private temp or the private home), relative contents and all, so the
    spelling leads inside to the projected interpreter; no directory that
    holds one is projected."""
    b = _base(tmp_path)
    if where == "private temp":
        monkeypatch.setattr(sandbox, "PRIVATE_TMP", str(b))
    elif where == "empty root":
        # Wherever the test's directory is, as /opt or /home/linuxbrew are.
        monkeypatch.setattr(sandbox, "PRIVATE_TMP", "/nonexistent-autoforge-tmp")
    prefix = b / "home" / ".linuxbrew" if where == "private home" else b / "brew"
    interpreter = _interpreter(b)
    spelled = _brew(prefix, interpreter)
    description = compile_launch(
        _request(b, projections=(_runtime(b),), spelled_paths=(spelled, spelled))
    )
    keg = prefix / "Cellar" / "python" / "3.14"
    inside = [m for m in description.mounts if m.target.startswith(f"{prefix}/")]
    assert inside == [
        Link(str(keg / "bin" / "python3"), interpreter),
        Link(str(prefix / "opt" / "python"), "../Cellar/python/3.14"),
    ]
    assert description.resolve_inside(spelled) == interpreter
    assert description.inside_path(str(keg / "bin" / "pip")) is None
    _parents_first(description)


@pytest.mark.parametrize("where", ["empty root", "private temp", "private home"])
def test_a_spelled_path_gets_each_directory_it_leaves_by_dotdot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, where: str
) -> None:
    """PR #232 R2-F1: links whose contents pass through a directory nothing
    projects and leave it by ``..`` (``../transit/../...``). The kernel
    leaves a directory only from inside it, so each one that nothing else
    makes is made inside, empty (``transit``; ``deep/sub``, whose parent it
    makes too), and the host's files in it stay hidden; one that a link
    below it makes (``via``) needs no entry of its own."""
    b = _base(tmp_path)
    if where == "private temp":
        monkeypatch.setattr(sandbox, "PRIVATE_TMP", str(b))
    elif where == "empty root":
        monkeypatch.setattr(sandbox, "PRIVATE_TMP", "/nonexistent-autoforge-tmp")
    top = b / "home" / "tools" if where == "private home" else b / "spelled"
    interpreter = _interpreter(b)
    up = os.path.relpath(interpreter, top)
    _file(top / "transit" / "notes.txt")
    _file(top / "deep" / "sub" / "notes.txt")
    _dir(top / "via")
    (top / "via" / "python3").symlink_to(interpreter)
    contents = {
        "python3": f"../transit/../{up}",
        "nested": f"../deep/sub/../../{up}",
        "back": "../via/../via/python3",
    }
    _dir(top / "alias")
    for name, points_to in contents.items():
        (top / "alias" / name).symlink_to(points_to)
    spelled = tuple(str(top / "alias" / name) for name in contents)
    description = compile_launch(_request(b, projections=(_runtime(b),), spelled_paths=spelled))
    inside = [m for m in description.mounts if m.target.startswith(f"{top}/")]
    assert inside == [
        *(Link(str(top / "alias" / name), contents[name]) for name in sorted(contents)),
        sandbox.Dir(str(top / "deep" / "sub")),
        sandbox.Dir(str(top / "transit")),
        Link(str(top / "via" / "python3"), interpreter),
    ]
    for path in spelled:
        assert description.resolve_inside(path) == interpreter
    assert description.inside_path(str(top / "transit" / "notes.txt")) is None
    opts = _options(description)
    assert opts[opts.index(str(top / "transit")) - 1] == "--dir"
    _parents_first(description)


def test_a_spelled_path_already_shown_needs_no_link(tmp_path: Path) -> None:
    """A link inside a projection is the host's own, read through it: the
    compiler adds nothing for it, and nothing for a path without a link."""
    b = _base(tmp_path)
    interpreter = _interpreter(b)
    tool = _dir(b / "tool")
    _dir(b / "tool" / "bin")
    (b / "tool" / "bin" / "python3").symlink_to("../../runtime/bin/python3.14")
    projections = (_runtime(b), P(tool, RO, DECLARED))
    plain = compile_launch(_request(b, projections=projections))
    spelled = (str(b / "tool" / "bin" / "python3"), interpreter)
    description = compile_launch(_request(b, projections=projections, spelled_paths=spelled))
    assert description.mounts == plain.mounts
    assert description.resolve_inside(spelled[0]) == interpreter


@pytest.mark.skipif(not os.path.islink("/proc/self"), reason="no /proc/self link on this host")
def test_a_spelled_path_through_a_kernel_filesystem_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SandboxPolicyError) as info:
        compile_launch(_request(tmp_path, spelled_paths=("/proc/self/exe",)))
    assert (info.value.path, info.value.rule) == ("/proc/self", "D13.1 spelled path")


def test_a_relocation_lands_in_the_private_home(tmp_path: Path) -> None:
    b = _base(tmp_path)
    real_home = _dir(b / "real-home")
    (b / "home-link").symlink_to(real_home)
    tool = _dir(b / "tool")
    description = compile_launch(
        _request(
            b,
            home=str(b / "home-link"),
            projections=(P(tool, RO, DECLARED, target=str(b / "home-link" / ".config" / "tool")),),
        )
    )
    assert Bind(tool, f"{real_home}/.config/tool", RO, True) in description.mounts
    assert Link(str(b / "home-link"), real_home) in description.mounts
    assert PrivateTmpfs(real_home, 0o700, None) in description.mounts
    assert dict(description.env_set)["HOME"] == str(b / "home-link")
    _parents_first(description)


def test_an_engine_overlay(tmp_path: Path) -> None:
    b = _base(tmp_path)
    common = _dir(b / "repo" / ".git")
    objects = _dir(b / "repo" / ".git" / "objects")
    upper = _dir(b / "repo" / ".git" / "autoforge" / "sandbox" / "7" / "objects")
    work = _dir(b / "repo" / ".git" / "autoforge" / "sandbox" / "7" / "work")
    description = compile_launch(
        _request(
            b,
            projections=(P(common),),
            masks=(f"{common}/autoforge",),
            overlays=(OverlayRequest(objects, upper, work, carve_out=True),),
            denied_roots=(f"{common}/autoforge",),
        )
    )
    assert Overlay(objects, upper, work, objects) in description.mounts


@pytest.mark.skipif(not os.path.isdir("/usr"), reason="no /usr on this host")
def test_the_system_runtime_is_read_only_in_the_hosts_form(tmp_path: Path) -> None:
    description = compile_launch(_request(tmp_path, system_runtime=True))
    usr = os.path.realpath("/usr")
    assert Bind(usr, usr, RO, True) in description.mounts
    for top in ("/bin", "/lib"):
        if os.path.islink(top):
            assert Link(top, os.readlink(top)) in description.mounts
    resolv = "/etc/resolv.conf"
    if os.path.islink(resolv) and os.path.isfile(resolv):
        target = os.path.realpath(resolv)
        if not target.startswith(("/etc/", "/usr/")):
            assert Bind(target, target, RO, False) in description.mounts
    assert all(mount.access is RO for mount in description.mounts if isinstance(mount, Bind))
    _parents_first(description)


@pytest.mark.parametrize(
    ("mounts", "env_set", "env_unset", "path"),
    [
        ((Bind("rel", "/x", RO, True),), (), (), "rel"),
        ((Mask("/a/../b"),), (), (), "/a/../b"),
        ((Mask("/a"), Mask("/a")), (), (), "/a"),
        ((Link("/bin", ""),), (), (), "/bin"),
        ((), (("A-B", "1"),), (), "A-B"),
        ((), (("A", "1"),), ("A",), "A"),
        ((), (("A", "x\0y"),), (), "env"),
    ],
)
def test_a_malformed_description_is_refused(
    mounts: tuple, env_set: tuple, env_unset: tuple, path: str
) -> None:
    with pytest.raises(SandboxPolicyError) as info:
        LaunchDescription(mounts=mounts, env_set=env_set, env_unset=env_unset)
    assert (info.value.path, info.value.rule) == (path, "malformed description")


# -------------------------------------------------------------------- facts


def test_facts_are_bounded_and_carry_no_value() -> None:
    facts = SandboxFacts(
        enforced=False,
        backend="bubblewrap",
        backend_version="v" * 200,
        network="host",
        tmpfs=(("/tmp", None), ("/home/op", 1024)),
        unset_env=tuple(f"NAME_{i}" for i in range(100)),
        setup_failure="x" * 5000,
    )
    record = facts.record()
    assert record["tmpfs"] == {"/tmp": "kernel default", "/home/op": 1024}
    assert len(record["backend_version"]) <= 64  # type: ignore[arg-type]
    assert len(record["setup_failure"]) <= 1000  # type: ignore[arg-type]
    assert len(record["unset_env"]) == 65  # type: ignore[arg-type]
    assert record["unset_env"][-1] == "... 36 more"  # type: ignore[index]
    json.dumps(record)


def test_the_teardown_fact_is_named_in_the_leftovers() -> None:
    clean = {"descendants_killed": False, "group_survived_kill": False, "capture_abandoned": False}
    assert describe_leftovers(**clean) == ""
    sentence = describe_leftovers(**clean, sandbox_teardown=True)
    assert "namespace was torn down" in sentence and "uncounted" in sentence
    assert TEARDOWN_UNCOUNTED == "removed by namespace teardown, uncounted"


# ------------------------------------------------------- the backend's options


_FULL = sandbox_bwrap._BackendInfo("0.12.0", has_size=True, has_perms=True, has_overlay=True)
_BARE = sandbox_bwrap._BackendInfo("0.4.0", has_size=False, has_perms=False, has_overlay=False)


def _options(description: LaunchDescription, info=_FULL, env=None, cwd=None) -> list[str]:
    return sandbox_bwrap._options(description, info, status_fd=9, cwd=cwd, env=env or {})


def test_every_launch_gets_the_namespaces_and_never_the_forbidden_flags(tmp_path: Path) -> None:
    opts = _options(compile_launch(_request(tmp_path)))
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--die-with-parent"):
        assert flag in opts
    assert opts[opts.index("--json-status-fd") + 1] == "9"
    assert opts[opts.index("--proc") + 1] == "/proc"
    assert opts[opts.index("--dev") + 1] == "/dev"
    assert ["--ro-bind", "/sys", "/sys"] == opts[opts.index("/sys") - 1 : opts.index("/sys") + 2]
    for never in (
        "--unshare-net",
        "--disable-userns",
        "--new-session",
        "--as-pid-1",
        "--unshare-all",
        "--share-net",
        "--dev-bind",
        "--cap-add",
    ):
        assert never not in opts


def test_an_isolated_network_and_an_absent_sys(tmp_path: Path) -> None:
    opts = _options(
        compile_launch(_request(tmp_path, network=NetworkPosition.ISOLATED, sys=SysPolicy.ABSENT))
    )
    assert "--unshare-net" in opts
    assert "/sys" not in opts


def test_masks_are_made_read_only_after_their_carve_outs(tmp_path: Path) -> None:
    b = _base(tmp_path)
    ws = _dir(b / "ws")
    carve = _dir(b / "ws" / "m" / "out")
    opts = _options(
        compile_launch(
            _request(b, projections=(P(ws), P(carve, RW, carve_out=True)), masks=(f"{ws}/m",))
        )
    )
    tmpfs = opts.index(f"{ws}/m")
    bind = opts.index(carve)
    remount = len(opts) - 1 - opts[::-1].index(f"{ws}/m")
    assert opts[tmpfs - 1] == "--tmpfs" and opts[tmpfs - 3 : tmpfs - 1] == ["--perms", "0755"]
    assert opts[bind - 1] == "--bind"
    assert opts[remount - 1] == "--remount-ro"
    assert tmpfs < bind < remount
    assert opts.index("--clearenv") > remount


def test_the_private_mounts_get_their_mode_and_bound_when_the_backend_has_them(
    tmp_path: Path,
) -> None:
    description = compile_launch(_request(tmp_path, tmp_size_bytes=1 << 20))
    full = _options(description)
    i = full.index("/tmp")
    assert full[i - 5 : i] == ["--perms", "1777", "--size", str(1 << 20), "--tmpfs"]
    bare = _options(description, info=_BARE)
    i = bare.index("/tmp")
    assert bare[i - 1] == "--tmpfs" and "--size" not in bare and "--perms" not in bare


def test_the_environment_is_cleared_then_set_exactly(tmp_path: Path) -> None:
    description = compile_launch(_request(tmp_path, env_unset=("SSH_AUTH_SOCK",)))
    env = sandbox_bwrap._environment(
        description, {"PATH": "/usr/bin", "SSH_AUTH_SOCK": "/agent", "HOME": "/real/home"}
    )
    opts = _options(description, env=env, cwd="/work")
    clear = opts.index("--clearenv")
    assert opts[clear - 2 : clear] == ["--chdir", "/work"]
    pairs = opts[clear + 1 :]
    assert pairs[0::3] == ["--setenv"] * len(env)
    assert dict(zip(pairs[1::3], pairs[2::3], strict=True)) == env
    assert "SSH_AUTH_SOCK" not in env and "XDG_RUNTIME_DIR" not in env
    assert env["HOME"] == dict(description.env_set)["HOME"]


# ----------------------------------------------------------------- drift guards


def _code_strings(path: Path) -> set[str]:
    """Every string literal in ``path`` that is not a docstring."""
    tree = ast.parse(path.read_text())
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    }


def test_no_bwrap_flag_outside_the_backend() -> None:
    # ``--version`` and ``--help`` are every CLI's, not the backend's.
    flags = {
        s for s in _code_strings(SRC / "sandbox_bwrap.py") if s.startswith("--") and len(s) > 2
    } - {"--version", "--help"}
    assert {"--ro-bind", "--unshare-pid", "--die-with-parent", "--json-status-fd"} <= flags
    for path in sorted(SRC.glob("*.py")):
        if path.name == "sandbox_bwrap.py":
            continue
        strings = _code_strings(path)
        assert not (strings & flags), (path.name, sorted(strings & flags))
        assert "bwrap" not in strings, path.name


def test_the_sandbox_modules_know_no_workflow() -> None:
    allowed = {"errors", "redaction", "sandbox", "executor"}
    for name in ("sandbox.py", "sandbox_bwrap.py"):
        tree = ast.parse((SRC / name).read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level:
                modules = [node.module] if node.module else [a.name for a in node.names]
                assert set(modules) <= allowed, (name, modules)


# --------------------------------------------- the executor, on a fake backend


FAKE_BWRAP = r'''#!@PYTHON@
"""A fake ``bwrap``: the options from ``--args``, the status records, the run."""
import json
import os
import subprocess
import sys

here = os.path.dirname(os.path.abspath(__file__))


def setting(name, default):
    try:
        with open(os.path.join(here, name)) as fh:
            return fh.read().strip()
    except OSError:
        return default


args = sys.argv[1:]
if args == ["--version"]:
    print("bubblewrap 0.99.0-fake")
    sys.exit(0)
if args == ["--help"]:
    print(setting("help", "--size BYTES --perms OCTAL --overlay RWSRC WORKDIR DEST"))
    sys.exit(0)
if args[0] != "--args":
    # The probe narrowing a namespace refusal: plain flags, then the command.
    with open(os.path.join(here, "narrow.jsonl"), "a") as fh:
        fh.write(json.dumps(args) + "\n")
    refused = setting("refuse", "").split()
    sys.exit(1 if any(flag in args for flag in refused) else 0)
assert args[0] == "--args" and args[2] == "--", args
fd = int(args[1])
data = b""
while True:
    chunk = os.read(fd, 65536)
    if not chunk:
        break
    data += chunk
os.close(fd)
options = [o.decode() for o in data.split(b"\0")[:-1]]
command = args[3:]
with open(os.path.join(here, "cmdline"), "ab") as fh:
    with open("/proc/self/cmdline", "rb") as own:
        fh.write(own.read() + b"\n")
with open(os.path.join(here, "log.jsonl"), "a") as fh:
    fh.write(json.dumps({"argv": sys.argv, "options": options, "environ": sorted(os.environ)}))
    fh.write("\n")
mode = setting("mode", "run")
if mode == "setup-fail":
    message = setting("stderr", "bwrap: Creating new namespace failed: Operation not permitted")
    sys.stderr.write(message + "\n")
    sys.exit(1)
status_fd = int(options[options.index("--json-status-fd") + 1])
env = {}
cwd = None
i = 0
while i < len(options):
    if options[i] == "--setenv":
        env[options[i + 1]] = options[i + 2]
        i += 3
        continue
    if options[i] == "--chdir":
        cwd = options[i + 1]
    i += 1
status = os.fdopen(status_fd, "w")
child = subprocess.Popen(command, env=env, cwd=cwd)
status.write(json.dumps({"child-pid": child.pid}) + "\n")
status.flush()
rc = child.wait()
code = rc if rc >= 0 else 128 - rc
status.write(json.dumps({"exit-code": code}) + "\n")
status.close()
sys.exit(code)
'''


_REAL_SCOPE = sandbox_bwrap._ProcessScope


class _NoScope:
    """The launch's process scope, stubbed: the fake backend tests the wiring."""

    def __init__(self) -> None:
        pass

    def apply(self) -> None:
        pass

    def failure(self) -> str:
        return ""

    def close(self) -> None:
        pass


class FakeBwrap:
    def __init__(self, root: Path) -> None:
        self.root = root

    def set(self, name: str, value: str) -> None:
        (self.root / name).write_text(value)

    def log(self) -> list[dict]:
        path = self.root / "log.jsonl"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines()]

    def cmdlines(self) -> bytes:
        path = self.root / "cmdline"
        return path.read_bytes() if path.exists() else b""


@pytest.fixture
def fake_bwrap(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeBwrap:
    root = tmp_path / "fakebin"
    root.mkdir()
    script = root / "bwrap"
    script.write_text(FAKE_BWRAP.replace("@PYTHON@", sys.executable))
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{root}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _NoScope)
    return FakeBwrap(root)


def _description(tmp_path: Path, **kw: object) -> LaunchDescription:
    return compile_launch(_request(tmp_path, **kw))


def _sh(script: str) -> list[str]:
    return ["/bin/sh", "-c", script]


@_linux_only
def test_the_command_runs_through_the_backend_and_records_the_facts(
    tmp_path: Path, fake_bwrap: FakeBwrap
) -> None:
    result = execute(
        ExecutionRequest(
            command=_sh("echo out; echo err >&2; exit 3"),
            env_allowlist=("PATH",),
            timeout_seconds=30,
            sandbox=_description(tmp_path, tmp_size_bytes=1 << 20),
        )
    )
    assert (result.exit_code, result.stdout, result.stderr) == (3, "out\n", "err\n")
    facts = result.sandbox
    assert facts is not None and facts.enforced
    assert (facts.backend, facts.backend_version) == ("bubblewrap", "0.99.0-fake")
    assert facts.teardown == TEARDOWN_UNCOUNTED
    assert facts.network == "host"
    assert dict(facts.tmpfs)["/tmp"] == 1 << 20
    assert facts.unset_env == ("XDG_RUNTIME_DIR",)
    assert "namespace was torn down" in result.leftovers


@_linux_only
def test_a_setup_failure_is_a_refusal_and_exit_one_is_not(
    tmp_path: Path, fake_bwrap: FakeBwrap
) -> None:
    description = _description(tmp_path)
    ran = execute(
        ExecutionRequest(
            command=_sh("echo partial; exit 1"), timeout_seconds=30, sandbox=description
        )
    )
    assert ran.exit_code == 1 and ran.sandbox is not None and ran.sandbox.enforced

    fake_bwrap.set("mode", "setup-fail")
    with pytest.raises(SandboxLaunchRefusedError) as info:
        execute(
            ExecutionRequest(
                command=_sh("echo partial; exit 1"), timeout_seconds=30, sandbox=description
            )
        )
    err = info.value
    assert isinstance(err, ExecutionError)
    assert "Creating new namespace failed" in str(err)
    facts = err.facts
    assert isinstance(facts, SandboxFacts) and not facts.enforced
    assert facts.setup_failure == "bwrap: Creating new namespace failed: Operation not permitted"
    assert facts.teardown == ""


@_linux_only
def test_the_setup_failure_reason_is_redacted_and_bounded() -> None:
    token = "gh" + "p_" + "A" * 36
    reason = sandbox_bwrap._setup_reason(f"one\ntwo\nbwrap: GITHUB_TOKEN={token}\n", 1)
    assert token not in reason and "***REDACTED***" in reason
    assert reason.startswith("one / two / ")
    assert len(sandbox_bwrap._setup_reason("x" * 5000, 1)) == 500
    assert sandbox_bwrap._setup_reason("", 7) == "exit status 7, no message"


@_linux_only
def test_no_environment_value_reaches_a_command_line(
    tmp_path: Path, fake_bwrap: FakeBwrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    value = "af167-env-value-on-no-command-line"
    monkeypatch.setenv("AF167_CONTROLLER_ONLY", "controller-only")
    result = execute(
        ExecutionRequest(
            command=[sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
            env_allowlist=("PATH",),
            env={"AF167_VALUE": value, "AF167_DROPPED": "x", "XDG_RUNTIME_DIR": "/run/user/0"},
            timeout_seconds=30,
            sandbox=_description(tmp_path, env_unset=("AF167_DROPPED",)),
        )
    )
    seen = json.loads(result.stdout)
    assert seen["AF167_VALUE"] == value
    for name in ("AF167_CONTROLLER_ONLY", "AF167_DROPPED", "XDG_RUNTIME_DIR"):
        assert name not in seen
    assert seen["HOME"] == str(_base(tmp_path) / "home") and seen["TMPDIR"] == "/tmp"
    (entry,) = fake_bwrap.log()
    assert value.encode() not in fake_bwrap.cmdlines()
    assert all(value not in arg for arg in entry["argv"])
    assert entry["options"][entry["options"].index("AF167_VALUE") + 1] == value
    assert "AF167_CONTROLLER_ONLY" not in entry["environ"]
    assert entry["argv"][1] == "--args" and entry["argv"][3] == "--"


@_linux_only
def test_the_launch_holds_no_descriptor_afterwards(tmp_path: Path, fake_bwrap: FakeBwrap) -> None:
    description = _description(tmp_path)
    execute(ExecutionRequest(command=["true"], timeout_seconds=30, sandbox=description))
    before = sorted(os.listdir("/proc/self/fd"))
    execute(ExecutionRequest(command=["true"], timeout_seconds=30, sandbox=description))
    fake_bwrap.set("mode", "setup-fail")
    with pytest.raises(SandboxLaunchRefusedError):
        execute(ExecutionRequest(command=["true"], timeout_seconds=30, sandbox=description))
    assert sorted(os.listdir("/proc/self/fd")) == before


@_linux_only
def test_a_refused_launch_leaves_the_containment_free(
    tmp_path: Path, fake_bwrap: FakeBwrap
) -> None:
    fake_bwrap.set("mode", "setup-fail")
    description = _description(tmp_path)
    with pytest.raises(SandboxLaunchRefusedError):
        execute(
            ExecutionRequest(
                command=["true"], timeout_seconds=30, sandbox=description, contain_orphans=True
            )
        )
    assert not executor._CONTAINMENT_LOCK.locked()
    result = execute(ExecutionRequest(command=["true"], timeout_seconds=30, contain_orphans=True))
    assert result.exit_code == 0


@_linux_only
def test_without_a_description_the_backend_is_never_consulted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def refuse(*_a: object, **_k: object) -> None:
        raise AssertionError("the backend was consulted")

    monkeypatch.setattr(sandbox_bwrap, "prepare", refuse)
    result = execute(ExecutionRequest(command=_sh("echo plain"), timeout_seconds=30))
    assert (result.stdout, result.sandbox) == ("plain\n", None)
    with start_duplex(DuplexRequest(command=_sh("echo plain"), stdin_pipe=False)) as child:
        assert child.read_line(10) == Record(b"plain")
        done = child.finish()
    assert done.sandbox is None and done.leftovers == ""


@_linux_only
def test_duplex_runs_through_the_backend(tmp_path: Path, fake_bwrap: FakeBwrap) -> None:
    echo = "import sys\nfor line in sys.stdin:\n    print(line.strip()[::-1], flush=True)\n"
    request = DuplexRequest(
        command=[sys.executable, "-c", echo],
        env_allowlist=("PATH",),
        max_runtime_seconds=30,
        sandbox=_description(tmp_path),
    )
    with start_duplex(request) as child:
        child.send_line(b"abc")
        assert child.read_line(10) == Record(b"cba")
        child.close_stdin()
        assert child.read_line(10) == Eof()
        result = child.finish()
    assert result.exit_code == 0
    assert result.sandbox is not None and result.sandbox.enforced
    assert "namespace was torn down" in result.leftovers


@_linux_only
def test_duplex_setup_failure_is_a_refusal_from_finish(
    tmp_path: Path, fake_bwrap: FakeBwrap
) -> None:
    fake_bwrap.set("mode", "setup-fail")
    request = DuplexRequest(
        command=_sh("cat"), max_runtime_seconds=30, sandbox=_description(tmp_path)
    )
    child = start_duplex(request)
    with pytest.raises(SandboxLaunchRefusedError) as info:
        with child:
            assert child.read_line(10) == Eof()
    assert "Creating new namespace failed" in str(info.value)
    assert not executor._CONTAINMENT_LOCK.locked()


@_linux_only
@pytest.mark.parametrize("duplex", [False, True])
def test_a_host_without_the_backend_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, duplex: bool
) -> None:
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    description = _description(tmp_path)
    with pytest.raises(SandboxLaunchRefusedError, match="bwrap was not found"):
        if duplex:
            start_duplex(DuplexRequest(command=["/bin/true"], sandbox=description))
        else:
            execute(ExecutionRequest(command=["/bin/true"], sandbox=description))


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_a_platform_without_a_backend_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, platform: str
) -> None:
    description = _description(tmp_path)
    monkeypatch.setattr(sandbox_bwrap, "sys", types.SimpleNamespace(platform=platform))
    with pytest.raises(SandboxLaunchRefusedError, match=f"no backend on {platform}"):
        execute(ExecutionRequest(command=["/bin/true"], sandbox=description))


@_linux_only
def test_a_backend_without_overlays_refuses_an_overlay(
    tmp_path: Path, fake_bwrap: FakeBwrap
) -> None:
    fake_bwrap.set("help", "--size BYTES --perms OCTAL")
    b = _base(tmp_path)
    description = _description(
        b, overlays=(OverlayRequest(_dir(b / "low"), _dir(b / "up"), _dir(b / "work")),)
    )
    with pytest.raises(SandboxLaunchRefusedError, match="cannot mount an overlay"):
        execute(ExecutionRequest(command=["/bin/true"], sandbox=description))
    assert fake_bwrap.log() == []


@_linux_only
def test_a_kernel_without_the_landlock_scope_refuses(
    tmp_path: Path, fake_bwrap: FakeBwrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _REAL_SCOPE)
    monkeypatch.setattr(sandbox_bwrap, "_landlock_abi", lambda _syscall: 5)
    with pytest.raises(SandboxLaunchRefusedError, match="Landlock ABI is 5.*needs ABI 6"):
        execute(ExecutionRequest(command=["/bin/true"], sandbox=_description(tmp_path)))
    assert fake_bwrap.log() == []


@_linux_only
def test_an_unknown_architecture_refuses(
    tmp_path: Path, fake_bwrap: FakeBwrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _REAL_SCOPE)
    monkeypatch.setattr(sandbox_bwrap.platform, "machine", lambda: "vax")
    with pytest.raises(SandboxLaunchRefusedError, match=r"architecture \(vax\)"):
        execute(ExecutionRequest(command=["/bin/true"], sandbox=_description(tmp_path)))


@_linux_only
def test_a_process_scope_that_fails_refuses_the_launch(
    tmp_path: Path, fake_bwrap: FakeBwrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    class Failing(_NoScope):
        def apply(self) -> None:
            raise OSError(1, "keyctl(KEYCTL_JOIN_SESSION_KEYRING)")

    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", Failing)
    with pytest.raises(SandboxLaunchRefusedError, match="session keyring") as info:
        execute(
            ExecutionRequest(
                command=["/bin/true"], sandbox=_description(tmp_path), contain_orphans=True
            )
        )
    assert isinstance(info.value.facts, SandboxFacts) and not info.value.facts.enforced
    assert fake_bwrap.log() == []
    assert not executor._CONTAINMENT_LOCK.locked()
    # A failure the scope did not record names no step: refused, capability unknown.
    assert sandbox_bwrap.classify(str(info.value)) is sandbox_bwrap.Capability.UNKNOWN


def _scope_failing_at(failing: str) -> type:
    """The real process scope, whose calls succeed but ``failing``, refused with EPERM.

    Its foreign calls are replaced by Python ones after the real
    ``__init__`` prepared their arguments, so ``apply`` runs as it does in
    a launch, in the forked child of a real ``Popen``, and only the kernel's
    answer is scripted (a kernel or a seccomp filter refusing the call).
    """

    class FailingAt(_REAL_SCOPE):  # type: ignore[misc, valid-type]
        def __init__(self) -> None:
            super().__init__()
            names = {
                self._join[0].value: "keyctl",
                self._create[0].value: "landlock_create_ruleset",
                self._restrict.value: "landlock_restrict_self",
            }

            def answer(name: str, ok: int) -> int:
                if name == failing:
                    ctypes.set_errno(errno.EPERM)
                    return -1
                return ok

            def syscall(number: ctypes.c_long, *_args: object) -> int:
                name = names[number.value]
                ruleset = name == "landlock_create_ruleset" and name != failing
                return answer(name, os.open(os.devnull, os.O_RDONLY) if ruleset else 0)

            self._syscall = syscall
            self._prctl = lambda *_args: answer("prctl", 0)

    return FailingAt


_SCOPE_FAILURES = [
    ("keyctl", "session_keyring", "keyctl(KEYCTL_JOIN_SESSION_KEYRING) failed (EPERM"),
    ("prctl", "landlock", "prctl(PR_SET_NO_NEW_PRIVS) failed (EPERM"),
    ("landlock_create_ruleset", "landlock", "landlock_create_ruleset failed (EPERM"),
    ("landlock_restrict_self", "landlock", "landlock_restrict_self failed (EPERM"),
]


@_linux_only
@pytest.mark.parametrize(("failing", "capability", "named"), _SCOPE_FAILURES)
def test_a_scope_step_that_fails_in_the_child_is_named_across_the_spawn(
    tmp_path: Path,
    fake_bwrap: FakeBwrap,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    capability: str,
    named: str,
) -> None:
    """The step that failed survives Python's generic preexec error (PR #231 R2-F4).

    Through a real ``Popen`` an exception raised between fork and exec
    reaches the controller as "Exception occurred in preexec_fn.", errno
    and call gone; the refusal still names the call and its errno, which
    the child recorded, and nothing ran.
    """
    monkeypatch.setattr(sandbox_bwrap, "_landlock_abi", lambda _syscall: 6)
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _scope_failing_at(failing))
    with pytest.raises(SandboxLaunchRefusedError) as info:
        execute(
            ExecutionRequest(
                command=["/bin/true"], sandbox=_description(tmp_path), contain_orphans=True
            )
        )
    assert named in str(info.value)
    assert "preexec_fn" not in str(info.value)
    assert sandbox_bwrap.classify(str(info.value)).key == capability
    facts = info.value.facts
    assert isinstance(facts, SandboxFacts) and not facts.enforced
    assert named in facts.setup_failure
    # Fail-closed: bwrap never ran, so neither did the command.
    assert fake_bwrap.log() == []
    assert not executor._CONTAINMENT_LOCK.locked()


@_linux_only
@pytest.mark.parametrize(("failing", "capability", "named"), _SCOPE_FAILURES)
def test_the_probe_tells_a_keyring_failure_from_a_landlock_one(
    tmp_path: Path,
    fake_bwrap: FakeBwrap,
    monkeypatch: pytest.MonkeyPatch,
    failing: str,
    capability: str,
    named: str,
) -> None:
    """The probe's launch, end to end, names which scope capability failed (PR #231 R2-F4)."""
    monkeypatch.setattr(sandbox_bwrap, "_landlock_abi", lambda _syscall: 6)
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _scope_failing_at(failing))
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert not result.supported
    assert result.capability is not None and result.capability.key == capability
    assert named in result.reason
    assert fake_bwrap.log() == []


# ------------------------------------------------------- how an end is read


def _launch(tmp_path: Path) -> sandbox_bwrap.BwrapLaunch:
    return sandbox_bwrap.BwrapLaunch(
        _description(tmp_path), _FULL, "/usr/bin/bwrap", ["/bin/true"], cwd=None, env={}
    )


@_linux_only
@pytest.mark.parametrize(
    ("records", "returncode", "by_executor", "refused"),
    [
        (b'{"child-pid": 2}\n{"exit-code": 1}\n', 1, False, False),
        (b'{"child-pid": 2}\n', 1, False, True),
        (b"", 1, False, True),
        (b"not json\n", 1, False, True),
        (b"", -9, False, False),
        (b"", 1, True, False),
        (b"", None, False, False),
    ],
)
def test_an_end_is_a_setup_failure_only_without_an_exit_record(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    records: bytes,
    returncode: int | None,
    by_executor: bool,
    refused: bool,
) -> None:
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _NoScope)
    launch = _launch(tmp_path)
    try:
        os.write(launch.pass_fds[1], records)
        launch.spawned()
        if refused:
            with pytest.raises(SandboxLaunchRefusedError):
                launch.outcome(returncode=returncode, ended_by_executor=by_executor, stderr="")
        else:
            facts = launch.outcome(returncode=returncode, ended_by_executor=by_executor, stderr="")
            assert facts.enforced
    finally:
        launch.close()
    launch.close()


@_linux_only
def test_the_options_travel_in_a_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(sandbox_bwrap, "_ProcessScope", _NoScope)
    launch = sandbox_bwrap.BwrapLaunch(
        _description(tmp_path),
        _FULL,
        "/usr/bin/bwrap",
        ["/bin/true"],
        cwd=None,
        env={"AF167_VALUE": "on-no-argv"},
    )
    try:
        args_fd, status_fd = launch.pass_fds
        assert launch.argv == ["/usr/bin/bwrap", "--args", str(args_fd), "--", "/bin/true"]
        assert launch.spawn_env == {}
        assert not any("on-no-argv" in arg for arg in launch.argv)
        assert os.get_inheritable(args_fd) is False
        with open(f"/proc/self/fd/{args_fd}", "rb") as fh:
            options = fh.read().split(b"\0")
        assert b"on-no-argv" in options
        assert options[options.index(b"--json-status-fd") + 1] == str(status_fd).encode()
    finally:
        launch.close()


# ------------------------------------------- the capability probe, typed (#169)


@pytest.mark.parametrize(
    ("reason", "capability"),
    [
        ("the outer sandbox has no backend on darwin; only Linux has one", "platform"),
        ("bwrap was not found on the controller's PATH", "backend"),
        ("/usr/bin/bwrap --version failed (exit 1)", "backend"),
        (
            "bubblewrap failed to set the sandbox up: bwrap: No permissions to create a new "
            "namespace, likely because the kernel does not allow non-privileged user "
            "namespaces. On e.g. debian this can be enabled with 'sysctl "
            "kernel.unprivileged_userns_clone=1'.",
            "user_namespace",
        ),
        (
            "bubblewrap failed to set the sandbox up: bwrap: setting up uid map: EPERM",
            "user_namespace",
        ),
        ("bwrap: Can't mount proc on /newroot/proc: Operation not permitted", "procfs"),
        ("bwrap: Can't bind mount /oldroot/srv on /newroot/srv: No such file", "bind"),
        ("bwrap: Can't find source path /gone: No such file or directory", "bind"),
        ("bwrap: Can't mount tmpfs on /newroot/tmp: Operation not permitted", "bind"),
        ("/usr/bin/bwrap 0.4.0 cannot mount an overlay", "overlay"),
        ("bwrap: Can't make overlay mount on /newroot/x with options ...: EINVAL", "overlay"),
        (
            "the kernel's Landlock ABI is 4; the abstract-socket scope needs ABI 6 (Linux "
            "6.12) with Landlock enabled",
            "landlock",
        ),
        (
            "the launch's process could not join a new session keyring or restrict itself to "
            "the Landlock abstract-socket scope ([Errno 1] landlock_restrict_self)",
            "landlock",
        ),
        (
            "the launch's process could not join a new session keyring or restrict itself to "
            "the Landlock abstract-socket scope ([Errno 1] keyctl(KEYCTL_JOIN_SESSION_KEYRING))",
            "session_keyring",
        ),
        ("bwrap: Creating new namespace failed: Operation not permitted", "unknown"),
        ("something no one has seen", "unknown"),
    ],
)
def test_a_refusal_names_the_capability_the_host_lacks(reason: str, capability: str) -> None:
    assert sandbox_bwrap.classify(reason).key == capability


def test_an_unsupported_probe_says_what_the_host_cannot_provide() -> None:
    result = sandbox_bwrap.ProbeResult(
        False, "bwrap: Can't mount proc", "", sandbox_bwrap.Capability.PROCFS
    )
    assert result.describe() == (
        "unsupported: this host cannot provide a new procfs (bwrap: Can't mount proc)"
    )
    assert sandbox_bwrap.ProbeResult(True, "", "0.12.0").describe() == (
        "supported (bubblewrap 0.12.0)"
    )


@_linux_only
def test_the_probe_passes_on_a_backend_that_runs_the_command(
    tmp_path: Path, fake_bwrap: FakeBwrap
) -> None:
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert result.supported and result.capability is None
    assert result.version == "0.99.0-fake"
    # The probe's description has a real launch's shape (D14.1).
    (launch,) = fake_bwrap.log()
    options = launch["options"]
    for flag in ("--unshare-user", "--unshare-pid", "--unshare-ipc", "--proc", "--dev"):
        assert flag in options, flag
    assert "--ro-bind" in options and "--overlay" in options


@_linux_only
@pytest.mark.parametrize(
    ("refuse", "capability"),
    [
        ("--unshare-user", "user_namespace"),
        ("--unshare-pid", "pid_namespace"),
        ("--unshare-ipc", "ipc_namespace"),
        ("", "unknown"),
    ],
)
def test_a_generic_namespace_refusal_is_narrowed_to_the_namespace(
    tmp_path: Path, fake_bwrap: FakeBwrap, refuse: str, capability: str
) -> None:
    fake_bwrap.set("mode", "setup-fail")
    fake_bwrap.set("refuse", refuse)
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert not result.supported
    assert result.capability is not None and result.capability.key == capability
    assert "Creating new namespace failed" in result.reason
    tried = [
        json.loads(line) for line in (fake_bwrap.root / "narrow.jsonl").read_text().splitlines()
    ]
    # One namespace more at each step, the host's root read-only, a trivial command.
    assert [t[: t.index("--ro-bind")] for t in tried][0] == ["--unshare-user"]
    assert all(t[t.index("--ro-bind") :][:3] == ["--ro-bind", "/", "/"] for t in tried)


@_linux_only
@pytest.mark.parametrize(
    ("stderr", "capability"),
    [
        ("bwrap: Can't mount proc on /newroot/proc: Operation not permitted", "procfs"),
        ("bwrap: Can't bind mount /oldroot/x on /newroot/x: Permission denied", "bind"),
        ("bwrap: Can't make overlay mount on /newroot/y with options x: EINVAL", "overlay"),
        (
            "bwrap: No permissions to create a new namespace, likely because the kernel does "
            "not allow non-privileged user namespaces.",
            "user_namespace",
        ),
    ],
)
def test_the_probe_names_the_failing_setup_step(
    tmp_path: Path, fake_bwrap: FakeBwrap, stderr: str, capability: str
) -> None:
    fake_bwrap.set("mode", "setup-fail")
    fake_bwrap.set("stderr", stderr)
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert (result.supported, result.capability.key if result.capability else None) == (
        False,
        capability,
    )
    assert not (fake_bwrap.root / "narrow.jsonl").exists()


@_linux_only
def test_the_probe_names_a_missing_backend_and_one_without_overlay(
    tmp_path: Path, fake_bwrap: FakeBwrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bwrap.set("help", "--size BYTES --perms OCTAL")
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert result.capability is sandbox_bwrap.Capability.OVERLAY
    empty = tmp_path / "empty-path"
    empty.mkdir()
    monkeypatch.setenv("PATH", str(empty))
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert result.capability is sandbox_bwrap.Capability.BACKEND
    assert "bwrap (bubblewrap)" in result.describe()


@_linux_only
def test_the_probe_writes_nothing_it_leaves_behind(
    tmp_path: Path, fake_bwrap: FakeBwrap, monkeypatch: pytest.MonkeyPatch
) -> None:
    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setenv("TMPDIR", str(scratch))
    import tempfile

    monkeypatch.setattr(tempfile, "tempdir", None)
    assert sandbox_bwrap.probe(home=str(tmp_path), environ={}).supported
    assert list(scratch.iterdir()) == []
