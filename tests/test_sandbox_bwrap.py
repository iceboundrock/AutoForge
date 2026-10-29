"""The outer sandbox on the real backend: Bubblewrap on this host (ADR 0005, #167).

Every test here launches real ``bwrap``. The session fixture runs the
backend's own probe (:func:`autoforge.sandbox_bwrap.probe`: the namespaces,
a mask with a carve-out, an overlay, a bounded temp, the Landlock scope and
the session keyring) and skips the module with the probe's reason when this
host cannot enforce a launch: no ``bwrap``, unprivileged user namespaces
refused (Ubuntu's AppArmor restriction), a kernel before Landlock ABI 6
(Linux 6.12), or no ``/usr/bin/python3`` to run the fake commands inside
(:func:`tests.conftest.real_sandbox_backend`; with
``AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1`` it fails instead). ``make
test-sandbox`` runs every real-backend module that way.

Everything inside is a small Python or shell script; every sentinel, socket
and repository is under ``tmp_path``; the operator's home is a fake one.
"""

from __future__ import annotations

import ctypes
import errno
import json
import os
import pwd
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest

from autoforge import executor, sandbox_bwrap
from autoforge.errors import SandboxLaunchRefusedError
from autoforge.executor import ExecutionRequest, ExecutionResult, execute
from autoforge.executor_duplex import DuplexRequest, Eof, Record, Timeout, start_duplex
from autoforge.sandbox import (
    TEARDOWN_UNCOUNTED,
    Access,
    LaunchDescription,
    NetworkPosition,
    Origin,
    OverlayRequest,
    ProjectionRequest,
    SandboxRequest,
    compile_launch,
)
from tests.conftest import SYSTEM_PYTHON, bind_unix, real_sandbox_backend

PY = SYSTEM_PYTHON
RO = Access.READ_ONLY
RW = Access.WRITABLE
ENGINE = Origin.ENGINE
DECLARED = Origin.DECLARED

# Prepended to every script run inside: an operation's outcome as a word.
_INNER = """
import errno, json, os, socket, sys
def attempt(fn):
    try:
        fn()
        return "ok"
    except OSError as exc:
        return errno.errorcode.get(exc.errno, str(exc.errno))
def write(path, text="x"):
    with open(path, "a") as fh:
        fh.write(text)
def read(path):
    with open(path) as fh:
        fh.read()
"""


@pytest.fixture(scope="session")
def backend(tmp_path_factory: pytest.TempPathFactory) -> str:
    return real_sandbox_backend(tmp_path_factory)


def _base(tmp_path: Path) -> Path:
    return Path(os.path.realpath(tmp_path))


def _dir(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _file(path: Path, text: str = "sentinel\n") -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return str(path)


def P(source: str, access: Access = RO, origin: Origin = ENGINE, **kw: object) -> ProjectionRequest:
    return ProjectionRequest(source, access, origin, **kw)  # type: ignore[arg-type]


def _describe(b: Path, **kw: object) -> LaunchDescription:
    kw.setdefault("home", _dir(b / "home"))
    kw.setdefault("controller_env", {"XDG_RUNTIME_DIR": _dir(b / "runtime")})
    return compile_launch(SandboxRequest(**kw))  # type: ignore[arg-type]


def _run(
    description: LaunchDescription, code: str, *, env: dict[str, str] | None = None, **kw: object
) -> ExecutionResult:
    kw.setdefault("timeout_seconds", 60)
    return execute(
        ExecutionRequest(
            command=[PY, "-c", _INNER + code],
            env_allowlist=("PATH",),
            env=env,
            sandbox=description,
            **kw,  # type: ignore[arg-type]
        )
    )


def _report(description: LaunchDescription, code: str, **kw: object) -> dict:
    result = _run(description, code, **kw)
    assert result.exit_code == 0, (result.stdout, result.stderr)
    assert result.sandbox is not None and result.sandbox.enforced
    return json.loads(result.stdout)


def _alive(token: str) -> list[int]:
    """Host processes whose command line carries ``token``."""
    found = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit() or int(entry) == os.getpid():
            continue
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as fh:
                if token.encode() in fh.read():
                    found.append(int(entry))
        except OSError:
            continue
    return found


# ------------------------------------------------------- what is reachable


def test_nothing_outside_the_projections_is_reachable(tmp_path: Path, backend: str) -> None:
    b = _base(tmp_path)
    home = b / "home"
    sentinels = [
        _file(home / ".config" / "gh" / "hosts.yml", "github.com:\n  oauth_token: fake\n"),
        _file(home / ".ssh" / "id_ed25519", "fake key\n"),
        _file(home / ".git-credentials", "https://fake@example.invalid\n"),
        _file(home / ".local" / "share" / "keyrings" / "login.keyring", "fake\n"),
    ]
    outside = _file(b / "outside" / "secret.txt")
    ws = _dir(b / "ws")
    os.symlink(outside, os.path.join(ws, "planted"))
    agent = b / "agent"
    agent.mkdir()
    listener = socket.socket(socket.AF_UNIX)
    bind_unix(listener, agent / "ssh-agent.sock")
    listener.listen(1)
    real_home = pwd.getpwuid(os.getuid()).pw_dir
    description = _describe(b, projections=(P(ws, RW),))
    try:
        seen = _report(
            description,
            f"""
def connect(path):  # by name from its directory, as conftest.bind_unix binds it
    s = socket.socket(socket.AF_UNIX)
    cwd = os.getcwd()
    try:
        os.chdir(os.path.dirname(path))
        s.connect(os.path.basename(path))
    finally:
        os.chdir(cwd)
        s.close()
print(json.dumps({{
    "sentinels": [attempt(lambda p=p: read(p)) for p in {sentinels!r}],
    "outside": attempt(lambda: read({outside!r})),
    "dotdot": attempt(lambda: read({ws!r} + "/../outside/secret.txt")),
    "planted": attempt(lambda: read({ws!r} + "/planted")),
    "agent": attempt(lambda: connect({str(agent / "ssh-agent.sock")!r})),
    "real_ssh": os.path.lexists({real_home!r} + "/.ssh"),
    "base": sorted(os.listdir({str(b)!r})),
    "home": sorted(os.listdir(os.environ["HOME"])),
}}))
""",
        )
    finally:
        listener.close()
    assert seen["sentinels"] == ["ENOENT"] * len(sentinels)
    assert seen["outside"] == seen["dotdot"] == seen["planted"] == "ENOENT"
    assert seen["agent"] == "ENOENT"
    assert seen["real_ssh"] is False
    assert seen["base"] == ["home", "ws"]
    assert seen["home"] == []


def test_read_only_writable_masked_and_carved_out(tmp_path: Path, backend: str) -> None:
    b = _base(tmp_path)
    ro = _dir(b / "ro")
    _file(b / "ro" / "f.txt", "ro\n")
    _file(b / "ro" / "masked" / "hidden.txt")
    carve = _dir(b / "ro" / "masked" / "out")
    rw = _dir(b / "rw")
    _file(b / "rw" / "g.txt", "rw\n")
    pinned = _file(b / "rw" / "pinned.txt", "pinned\n")
    single = _file(b / "single.txt", "one\n")
    description = _describe(
        b,
        projections=(
            P(ro),
            P(carve, RW, carve_out=True),
            P(rw, RW),
            P(pinned),
            P(single, RW),
        ),
        masks=(f"{ro}/masked",),
    )
    seen = _report(
        description,
        f"""
ro, rw, single, pinned = {ro!r}, {rw!r}, {single!r}, {pinned!r}
print(json.dumps({{
    "ro_read": attempt(lambda: read(ro + "/f.txt")),
    "ro_write": attempt(lambda: write(ro + "/f.txt")),
    "ro_create": attempt(lambda: write(ro + "/new.txt")),
    "ro_unlink": attempt(lambda: os.unlink(ro + "/f.txt")),
    "ro_rename": attempt(lambda: os.rename(ro + "/f.txt", ro + "/moved.txt")),
    "ro_mkdir": attempt(lambda: os.mkdir(ro + "/d")),
    "masked": sorted(os.listdir(ro + "/masked")),
    "mask_create": attempt(lambda: write(ro + "/masked/new.txt")),
    "carve_write": attempt(lambda: write(ro + "/masked/out/made.txt", "carved")),
    "rw_write": attempt(lambda: write(rw + "/g.txt", "more")),
    "rw_rename": attempt(lambda: os.rename(rw + "/g.txt", rw + "/h.txt")),
    "rw_unlink": attempt(lambda: os.unlink(rw + "/h.txt")),
    "rw_create": attempt(lambda: write(rw + "/new.txt", "new")),
    "hardlink": attempt(lambda: os.link(ro + "/f.txt", rw + "/linked.txt")),
    "pinned_write": attempt(lambda: write(pinned)),
    "pinned_rename": attempt(lambda: os.rename(pinned, rw + "/unpinned.txt")),
    "pinned_unlink": attempt(lambda: os.unlink(pinned)),
    "single_write": attempt(lambda: write(single, "two")),
    "single_rename": attempt(lambda: os.rename(single, single + ".moved")),
    "single_replace": attempt(lambda: (write(single + ".tmp"), os.rename(single + ".tmp", single))),
}}))
""",
    )
    assert seen["ro_read"] == "ok"
    for op in ("ro_write", "ro_create", "ro_unlink", "ro_rename", "ro_mkdir", "mask_create"):
        assert seen[op] == "EROFS", op
    assert seen["masked"] == ["out"]
    for op in ("carve_write", "rw_write", "rw_rename", "rw_unlink", "rw_create", "single_write"):
        assert seen[op] == "ok", op
    assert seen["hardlink"] == "EXDEV"
    # A read-only file pinned inside a writable projection (a worktree's
    # ``commondir``): neither rewritten nor moved aside.
    assert seen["pinned_write"] == "EROFS"
    assert seen["pinned_rename"] == seen["pinned_unlink"] == "EBUSY"
    # D7.1's reason a writable declaration is a directory: a file bind is a
    # mount point, which rename refuses.
    assert seen["single_rename"] == "EBUSY"
    assert seen["single_replace"] in ("EBUSY", "EROFS")
    assert (b / "ro" / "f.txt").read_text() == "ro\n"
    assert (b / "ro" / "masked" / "hidden.txt").exists()
    assert (b / "ro" / "masked" / "out" / "made.txt").read_text() == "carved"
    assert sorted(os.listdir(rw)) == ["new.txt", "pinned.txt"]
    assert (b / "rw" / "pinned.txt").read_text() == "pinned\n"
    assert (b / "single.txt").read_text() == "one\ntwo"


def test_an_overlay_keeps_its_lower_layer(tmp_path: Path, backend: str) -> None:
    b = _base(tmp_path)
    low = _dir(b / "low")
    _file(b / "low" / "a.txt", "a\n")
    _file(b / "low" / "b.txt", "b\n")
    up = _dir(b / "up")
    work = _dir(b / "work")
    description = _describe(b, overlays=(OverlayRequest(low, up, work),))
    try:
        seen = _report(
            description,
            f"""
low = {low!r}
print(json.dumps({{
    "modify": attempt(lambda: write(low + "/a.txt", "changed")),
    "create": attempt(lambda: write(low + "/new.txt", "new")),
    "delete": attempt(lambda: os.unlink(low + "/b.txt")),
    "listing": sorted(os.listdir(low)),
}}))
""",
        )
    finally:
        sandbox_bwrap._make_removable(work)
    assert seen == {"modify": "ok", "create": "ok", "delete": "ok", "listing": ["a.txt", "new.txt"]}
    assert sorted(os.listdir(low)) == ["a.txt", "b.txt"]
    assert (b / "low" / "a.txt").read_text() == "a\n"
    assert (b / "up" / "a.txt").read_text() == "a\nchanged"
    assert (b / "up" / "new.txt").read_text() == "new"
    whiteout = os.lstat(b / "up" / "b.txt")
    assert stat.S_ISCHR(whiteout.st_mode) and whiteout.st_rdev == 0


def test_the_private_temp_is_bounded(tmp_path: Path, backend: str) -> None:
    bwrap = shutil.which("bwrap")
    assert bwrap is not None
    if not sandbox_bwrap._backend_info(bwrap).has_size:
        pytest.skip(f"bwrap {backend} has no --size")
    b = _base(tmp_path)
    description = _describe(b, tmp_size_bytes=1 << 20)
    result = _run(
        description,
        """
print(json.dumps({
    "big": attempt(lambda: write("/tmp/big", "x" * (2 << 20))),
    "tmpdir": os.environ["TMPDIR"],
}))
""",
    )
    assert json.loads(result.stdout) == {"big": "ENOSPC", "tmpdir": "/tmp"}
    assert result.sandbox is not None
    assert result.sandbox.record()["tmpfs"]["/tmp"] == 1 << 20  # type: ignore[index]


# --------------------------------------------------------- what is shared


def test_ipc_keyring_and_abstract_sockets_are_the_sandboxes_own(
    tmp_path: Path, backend: str
) -> None:
    b = _base(tmp_path)
    description = _describe(b)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.syscall.restype = ctypes.c_long
    shmid = libc.shmget(0, 4096, 0o600 | 0o1000)  # IPC_PRIVATE, IPC_CREAT
    assert shmid >= 0
    name = f"\0autoforge-test-{uuid.uuid4().hex}"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(name)
    listener.listen(1)
    keyctl = sandbox_bwrap._SYS_KEYCTL[os.uname().machine]
    session = libc.syscall(keyctl, 0, ctypes.c_long(-3), 0)  # GET_KEYRING_ID, @s, no create
    code = f"""
import ctypes
libc = ctypes.CDLL(None)
libc.syscall.restype = ctypes.c_long
def connect(name):
    s = socket.socket(socket.AF_UNIX)
    try:
        s.connect(name)
    finally:
        s.close()
inner = socket.socket(socket.AF_UNIX)
inner.bind("\\0inner")
inner.listen(1)
print(json.dumps({{
    "shm": open("/proc/sysvipc/shm").read().count("\\n"),
    "host_abstract": attempt(lambda: connect({name!r})),
    "inner_abstract": attempt(lambda: connect("\\0inner")),
    "session_keyring": libc.syscall({keyctl}, 0, ctypes.c_long(-3), 0),
}}))
"""
    try:
        first = _report(description, code)
        second = _report(description, code)
        check = socket.socket(socket.AF_UNIX)
        check.connect(name)
        check.close()
    finally:
        listener.close()
        libc.shmctl(shmid, 0, None)  # IPC_RMID
    assert first["shm"] == 1  # the header line only
    assert first["host_abstract"] == "EPERM"
    assert first["inner_abstract"] == "ok"
    assert first["session_keyring"] > 0 and second["session_keyring"] > 0
    assert first["session_keyring"] != second["session_keyring"]
    assert session not in (first["session_keyring"], second["session_keyring"])
    assert libc.syscall(keyctl, 0, ctypes.c_long(-3), 0) == session


# add_key(2) and keyctl(2) for the architectures the helper below runs on.
_SYS_ADD_KEY = {"x86_64": 248, "aarch64": 217}

_KEY_HELPER = """
import ctypes, json, os, sys
sys.path.insert(0, sys.argv[1])
from autoforge import executor, sandbox
libc = ctypes.CDLL(None, use_errno=True)
libc.syscall.restype = ctypes.c_long
add_key, keyctl, name, home = int(sys.argv[2]), int(sys.argv[3]), sys.argv[4], sys.argv[5]
# A session keyring of this helper's own, so the operator's is never touched.
assert libc.syscall(keyctl, 1, None) > 0  # KEYCTL_JOIN_SESSION_KEYRING
key = libc.syscall(add_key, b"user", name.encode(), b"fake-secret", 11, ctypes.c_long(-3))
found = libc.syscall(keyctl, 10, ctypes.c_long(-3), b"user", name.encode(), 0)  # SEARCH @s
inner = (
    "import ctypes, errno\\n"
    "libc = ctypes.CDLL(None, use_errno=True)\\n"
    "libc.syscall.restype = ctypes.c_long\\n"
    f"rc = libc.syscall({keyctl}, 10, ctypes.c_long(-3), b'user', {name.encode()!r}, 0)\\n"
    "print(rc if rc >= 0 else errno.errorcode[ctypes.get_errno()])\\n"
)
description = sandbox.compile_launch(
    sandbox.SandboxRequest(home=home, controller_env={})
)
result = executor.execute(
    executor.ExecutionRequest(
        command=["/usr/bin/python3", "-c", inner],
        env_allowlist=("PATH",),
        timeout_seconds=60,
        sandbox=description,
    )
)
print(json.dumps({"key": key, "found": found, "inside": result.stdout.strip()}))
"""


def test_a_session_keyring_key_on_the_host_is_unreachable_inside(
    tmp_path: Path, backend: str
) -> None:
    machine = os.uname().machine
    if machine not in _SYS_ADD_KEY:
        pytest.skip(f"no add_key number for {machine}")
    src = str(Path(__file__).resolve().parents[1] / "src")
    name = f"autoforge-test-{uuid.uuid4().hex}"
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            _KEY_HELPER,
            src,
            str(_SYS_ADD_KEY[machine]),
            str(sandbox_bwrap._SYS_KEYCTL[machine]),
            name,
            _dir(_base(tmp_path) / "home"),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=True,
    )
    seen = json.loads(out.stdout)
    assert seen["key"] > 0 and seen["found"] == seen["key"]
    assert seen["inside"] == "ENOKEY"


def test_an_isolated_network_has_only_loopback(tmp_path: Path, backend: str) -> None:
    description = _describe(_base(tmp_path), network=NetworkPosition.ISOLATED)
    seen = _report(description, "print(json.dumps([n for _, n in socket.if_nameindex()]))")
    assert seen == ["lo"]
    assert description.network.value == "isolated"


def test_a_nested_namespace_cannot_unmask_or_remount(tmp_path: Path, backend: str) -> None:
    b = _base(tmp_path)
    ro = _dir(b / "ro")
    _file(b / "ro" / "f.txt")
    _file(b / "ro" / "masked" / "hidden.txt")
    description = _describe(b, projections=(P(ro),), masks=(f"{ro}/masked",))
    seen = _report(
        description,
        f"""
import ctypes
libc = ctypes.CDLL(None, use_errno=True)
def call(fn, *args):
    rc = fn(*args)
    return "ok" if rc == 0 else errno.errorcode.get(ctypes.get_errno(), "?")
ro = {ro!r}
nested = call(libc.unshare, 0x10000000 | 0x00020000)  # CLONE_NEWUSER | CLONE_NEWNS
print(json.dumps({{
    "nested": nested,
    "umount": call(libc.umount2, (ro + "/masked").encode(), 2),  # MNT_DETACH
    "remount": call(libc.mount, None, ro.encode(), None, 32 | 4096, None),  # REMOUNT|BIND
    "masked": sorted(os.listdir(ro + "/masked")),
    "write": attempt(lambda: write(ro + "/f.txt")),
}}))
""",
    )
    if seen["nested"] == "ok":
        assert seen["umount"] == "EINVAL"
        assert seen["remount"] == "EPERM"
    assert seen["masked"] == []
    assert seen["write"] == "EROFS"


# ------------------------------------------------------------------- git


def _git_env(home: str) -> dict[str, str]:
    return {
        "PATH": "/usr/bin:/bin",
        "HOME": home,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "AutoForge Test",
        "GIT_AUTHOR_EMAIL": "test@example.invalid",
        "GIT_COMMITTER_NAME": "AutoForge Test",
        "GIT_COMMITTER_EMAIL": "test@example.invalid",
    }


@pytest.mark.parametrize("ref_format", ["files", "reftable"])
def test_the_git_common_dir_layout(tmp_path: Path, backend: str, ref_format: str) -> None:
    """ADR 0005 D6.1's layout for a linked worktree, both ref backends."""
    git = "/usr/bin/git"
    if not os.path.exists(git):
        pytest.skip("/usr/bin/git is not installed")
    b = _base(tmp_path)
    home = _dir(b / "home")
    env = _git_env(home)

    def host(*args: str, cwd: str | None = None) -> str:
        out = subprocess.run(
            [git, *args], cwd=cwd, env=env, check=True, capture_output=True, text=True
        )
        return out.stdout.strip()

    main = str(b / "repo")
    init = subprocess.run(
        [git, "init", "-q", "-b", "main", f"--ref-format={ref_format}", main],
        env=env,
        capture_output=True,
        text=True,
    )
    if init.returncode != 0:
        pytest.skip(f"git cannot create a {ref_format} repository: {init.stderr.strip()}")
    _file(b / "repo" / "f.txt", "one\n")
    host("add", "f.txt", cwd=main)
    host("commit", "-qm", "one", cwd=main)
    common = f"{main}/.git"
    _file(Path(common) / "autoforge" / "controller.lock", "held\n")
    worktree = f"{common}/autoforge/worktrees/7"
    host("worktree", "add", "-q", "--detach", worktree, "main", cwd=main)
    host("worktree", "add", "-q", "--detach", str(b / "other"), "main", cwd=main)
    admin = f"{common}/worktrees/7"
    assert Path(f"{admin}/gitdir").read_text().strip() == f"{worktree}/.git"
    private = _dir(Path(common) / "autoforge" / "sandbox" / "7" / "objects")
    work = _dir(Path(common) / "autoforge" / "sandbox" / "7" / "overlay-work")
    base_commit = host("rev-parse", "main", cwd=main)
    description = _describe(
        b,
        home=home,
        projections=(
            P(common),
            P(worktree, RW, carve_out=True),
            P(admin, RW, carve_out=True),
            P(f"{admin}/commondir"),
            P(f"{admin}/gitdir"),
        ),
        masks=(f"{common}/autoforge", f"{common}/worktrees"),
        overlays=(OverlayRequest(f"{common}/objects", private, work, carve_out=True),),
        denied_roots=(f"{common}/autoforge",),
    )
    script = f"""
set -u
cd {worktree}
r() {{ if "$@" >/dev/null 2>&1; then echo ok; else echo refused; fi; }}
echo "status=$(r git status --short)"
echo two >> f.txt
echo "commit=$(r sh -c 'git add f.txt && git commit -qm two')"
echo "worktree_ref=$(r git update-ref refs/worktree/keep HEAD)"
echo "branch=$(r git branch feature)"
echo "config=$(r git config core.hooksPath /tmp)"
echo "hook=$(r sh -c 'echo x > {common}/hooks/pre-commit')"
echo "packed_refs=$(r sh -c 'echo x >> {common}/packed-refs')"
echo "heads=$(r sh -c 'echo {"0" * 40} > {common}/refs/heads/main')"
echo "alternates=$(r sh -c 'echo /tmp > {common}/objects/info/alternates')"
echo "commondir=$(r sh -c 'echo /tmp > {admin}/commondir')"
echo "autoforge=$(ls -A {common}/autoforge | tr '\\n' ' ')"
echo "admin_dirs=$(ls -A {common}/worktrees | tr '\\n' ' ')"
"""
    try:
        result = execute(
            ExecutionRequest(
                command=["/bin/sh", "-c", script],
                env_allowlist=(),
                env=_git_env(home),
                timeout_seconds=60,
                sandbox=description,
            )
        )
    finally:
        sandbox_bwrap._make_removable(work)
    assert result.exit_code == 0, result.stderr
    seen = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert seen["status"] == seen["commit"] == seen["worktree_ref"] == "ok"
    for refused in ("branch", "config", "hook", "packed_refs", "heads", "commondir"):
        assert seen[refused] == "refused", refused
    # A write to the alternates lands in the private object directory.
    assert seen["alternates"] == "ok"
    assert not os.path.exists(f"{common}/objects/info/alternates")
    assert os.path.exists(f"{private}/info/alternates")
    assert seen["autoforge"].split() == ["worktrees"]
    assert seen["admin_dirs"].split() == ["7"]
    # On the host: the operator's branch did not move, the candidate is read
    # through the common dir, and its objects are only in the private store.
    assert host("rev-parse", "main", cwd=main) == base_commit
    candidate = host("--git-dir", common, "rev-parse", "worktrees/7/HEAD")
    assert candidate != base_commit
    loose = f"{candidate[:2]}/{candidate[2:]}"
    assert os.path.exists(f"{private}/{loose}")
    assert not os.path.exists(f"{common}/objects/{loose}")
    assert host("--git-dir", common, "branch", "--list") == "* main"


# ---------------------------------------------------------------- processes


def test_host_processes_are_neither_visible_nor_signallable(tmp_path: Path, backend: str) -> None:
    seen = _report(
        _describe(_base(tmp_path)),
        f"""
print(json.dumps({{
    "pids": sorted(int(p) for p in os.listdir("/proc") if p.isdigit()),
    "signal": attempt(lambda: os.kill({os.getpid()}, 0)),
}}))
""",
    )
    assert seen["signal"] == "ESRCH"
    assert len(seen["pids"]) <= 3 and max(seen["pids"]) < 100


def _leaver(token: str, then: str) -> list[str]:
    """A command that leaves a ``setsid`` daemon and a background child that
    ignores SIGTERM, then ends as ``then`` says."""
    return [
        "/bin/sh",
        "-c",
        f"setsid {PY} -c 'import time; time.sleep(600)' {token}-setsid & "
        f"{PY} -c 'import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        f"time.sleep(600)' {token}-background & "
        f"sleep 0.5; {then}",
    ]


@pytest.mark.parametrize("contain", [False, True])
@pytest.mark.parametrize("end", ["exit", "timeout", "stop"])
def test_nothing_the_command_left_inside_survives(
    tmp_path: Path, backend: str, end: str, contain: bool
) -> None:
    token = f"autoforge-test-{uuid.uuid4().hex}"
    stop = threading.Event()
    timer = threading.Timer(1.5, stop.set)
    request = ExecutionRequest(
        command=_leaver(token, "exit 3" if end == "exit" else "sleep 600"),
        env_allowlist=("PATH",),
        timeout_seconds=1 if end == "timeout" else 60,
        contain_orphans=contain,
        stop=stop if end == "stop" else None,
        sandbox=_describe(_base(tmp_path)),
    )
    started = time.monotonic()
    if end == "stop":
        timer.start()
    try:
        result = execute(request)
    finally:
        timer.cancel()
    elapsed = time.monotonic() - started
    assert _alive(token) == []
    assert result.exit_code == (3 if end == "exit" else -1)
    assert result.timed_out is (end == "timeout")
    assert result.stopped is (end == "stop")
    assert result.sandbox is not None and result.sandbox.enforced
    assert result.sandbox.teardown == TEARDOWN_UNCOUNTED
    assert "namespace was torn down, uncounted" in result.leftovers
    assert not (result.group_survived_kill or result.capture_abandoned)
    assert not (result.orphans_killed or result.orphan_survived_kill)
    # The kernel removes what is inside at once: no member waits out a grace.
    assert elapsed < executor._KILL_GRACE_SECONDS


@pytest.mark.parametrize("contain", [False, True])
def test_a_command_that_kills_its_own_group_ended_abnormally(
    tmp_path: Path, backend: str, contain: bool
) -> None:
    """D10.8: ``bwrap`` killed by a signal the executor did not send is the
    command's end, never a setup failure."""
    result = execute(
        ExecutionRequest(
            command=["/bin/sh", "-c", "kill -KILL 0"],
            env_allowlist=("PATH",),
            timeout_seconds=60,
            contain_orphans=contain,
            sandbox=_describe(_base(tmp_path)),
        )
    )
    assert result.exit_code == -signal.SIGKILL
    assert result.sandbox is not None and result.sandbox.enforced
    assert not (result.descendants_killed or result.group_survived_kill)


_ECHO = (
    "import sys\nprint('ready', flush=True)\n"
    "for line in sys.stdin:\n    print(line.strip()[::-1], flush=True)\n"
)


def test_duplex_round_trip_and_shutdown_through_the_backend(tmp_path: Path, backend: str) -> None:
    request = DuplexRequest(
        command=[PY, "-c", _ECHO],
        env_allowlist=("PATH",),
        max_runtime_seconds=60,
        contain_orphans=True,
        sandbox=_describe(_base(tmp_path)),
    )
    with start_duplex(request) as child:
        assert child.read_line(30) == Record(b"ready")
        child.send_line(b"abc")
        assert child.read_line(30) == Record(b"cba")
        child.close_stdin()
        assert child.read_line(30) == Eof()
        result = child.finish()
    assert result.exit_code == 0 and not result.timed_out
    assert result.sandbox is not None and result.sandbox.enforced
    assert "namespace was torn down" in result.leftovers


def test_a_duplex_deadline_kills_everything_inside(tmp_path: Path, backend: str) -> None:
    token = f"autoforge-test-{uuid.uuid4().hex}"
    request = DuplexRequest(
        command=_leaver(token, "sleep 600"),
        env_allowlist=("PATH",),
        max_runtime_seconds=1,
        sandbox=_describe(_base(tmp_path)),
    )
    with start_duplex(request) as child:
        assert child.read_line(30) == Timeout(deadline_exceeded=True)
        result = child.finish()
    assert result.timed_out and result.exit_code == -1
    assert _alive(token) == []
    assert not (result.group_survived_kill or result.capture_abandoned)


def test_a_sandbox_dies_with_the_thread_that_spawned_it(tmp_path: Path, backend: str) -> None:
    """D10.6: ``--die-with-parent`` fires when the spawning *thread* exits,
    so a sandboxed launch is spawned from a thread that outlives it. This
    pins the hazard: a duplex started from a thread that then exits is
    killed with it, and that is the command's abnormal end, not a refusal."""
    holder: dict[str, object] = {}

    def start() -> None:
        child = start_duplex(
            DuplexRequest(
                command=[PY, "-c", _ECHO],
                env_allowlist=("PATH",),
                max_runtime_seconds=60,
                sandbox=_describe(_base(tmp_path)),
            )
        )
        holder["child"] = child
        holder["first"] = child.read_line(30)

    thread = threading.Thread(target=start)
    thread.start()
    thread.join()
    child = holder["child"]
    assert holder["first"] == Record(b"ready")
    deadline = time.monotonic() + 30
    while not child.exited() and time.monotonic() < deadline:  # type: ignore[attr-defined]
        time.sleep(0.05)
    result = child.finish()  # type: ignore[attr-defined]
    assert result.exit_code == -signal.SIGKILL
    assert result.sandbox is not None and result.sandbox.enforced


# ----------------------------------------------------------- setup failures


def test_a_missing_bind_source_is_a_refusal(tmp_path: Path, backend: str) -> None:
    b = _base(tmp_path)
    ws = _dir(b / "ws")
    description = _describe(b, projections=(P(ws, RW),))
    os.rmdir(ws)
    with pytest.raises(SandboxLaunchRefusedError) as info:
        _run(description, "print('ran')")
    facts = info.value.facts
    assert facts is not None and not facts.enforced  # type: ignore[attr-defined]
    assert ws in facts.setup_failure  # type: ignore[attr-defined]
    assert "ran" not in str(info.value)


@pytest.mark.parametrize("duplex", [False, True])
def test_an_executable_the_sandbox_cannot_run_is_a_refusal_and_exit_one_is_not(
    tmp_path: Path, backend: str, duplex: bool
) -> None:
    description = _describe(_base(tmp_path))
    missing = "/usr/bin/autoforge-no-such-command"
    if duplex:
        with pytest.raises(SandboxLaunchRefusedError, match="execvp"):
            with start_duplex(DuplexRequest(command=[missing], sandbox=description)) as child:
                assert child.read_line(30) == Eof()
    else:
        with pytest.raises(SandboxLaunchRefusedError, match="execvp"):
            execute(ExecutionRequest(command=[missing], sandbox=description))
    ran = execute(ExecutionRequest(command=["/bin/sh", "-c", "exit 1"], sandbox=description))
    assert ran.exit_code == 1 and ran.sandbox is not None and ran.sandbox.enforced


# ---------------------------------------------------------- what is handed in


def test_only_the_standard_descriptors_and_no_value_on_a_command_line(
    tmp_path: Path, backend: str
) -> None:
    value = f"autoforge-value-{uuid.uuid4().hex}"
    seen = _report(
        _describe(_base(tmp_path)),
        """
fds = sorted(int(fd) for fd in os.listdir("/proc/self/fd"))
cmdlines = b"".join(
    open(f"/proc/{p}/cmdline", "rb").read() for p in os.listdir("/proc") if p.isdigit()
)
print(json.dumps({
    "fds": fds,
    "on_cmdline": os.environ["AF_VALUE"].encode() in cmdlines,
    "value": os.environ["AF_VALUE"],
    "names": sorted(os.environ),
}))
""",
        env={"AF_VALUE": value, "SSH_AUTH_SOCK": "/agent.sock"},
    )
    assert seen["fds"][:3] == [0, 1, 2] and len(seen["fds"]) == 4  # the listing's own
    assert seen["value"] == value and seen["on_cmdline"] is False
    assert "XDG_RUNTIME_DIR" not in seen["names"]
    assert set(seen["names"]) >= {"HOME", "TMPDIR", "XDG_CONFIG_HOME", "PATH", "AF_VALUE"}


def test_the_controller_keeps_neither_the_keyring_nor_the_landlock_domain(
    tmp_path: Path, backend: str
) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    no_new_privs = libc.prctl(39, 0, 0, 0, 0)  # PR_GET_NO_NEW_PRIVS
    name = f"\0autoforge-test-{uuid.uuid4().hex}"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(name)
    listener.listen(1)
    try:
        _report(_describe(_base(tmp_path)), "print('{}')")
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(name)
        except OSError as exc:
            pytest.fail(f"the controller lost its abstract sockets: {errno.errorcode[exc.errno]}")
        finally:
            probe.close()
    finally:
        listener.close()
    assert libc.prctl(39, 0, 0, 0, 0) == no_new_privs


def test_the_probe_finds_this_host_capable_and_leaves_nothing(
    backend: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real probe (#169, D14.1): supported, the backend's version, and its
    temporary directory gone, overlay work directory included."""
    import tempfile

    scratch = tmp_path / "tmp"
    scratch.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(scratch))
    result = sandbox_bwrap.probe(home=str(tmp_path), environ={})
    assert result.supported and result.capability is None and result.reason == ""
    assert result.describe() == f"supported (bubblewrap {backend})"
    assert list(scratch.iterdir()) == []
