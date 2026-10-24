"""The launch-class mapping and the provider declarations (ADR 0005 §2.5, D7, D8.2, D13).

No launch here: every test compiles a description from real paths under
``tmp_path`` and reads it. The real backend runs them in
``test_sandbox_routing.py``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from autoforge.config import ProfileConfig
from autoforge.errors import SandboxLaunchRefusedError
from autoforge.providers import ClaudeCodeProvider, OpenCodeProvider, PiProvider
from autoforge.sandbox import (
    Access,
    Bind,
    LaunchDescription,
    Link,
    Mask,
    NetworkPosition,
    Overlay,
    PrivateTmpfs,
)
from autoforge.sandbox_policy import (
    AuthItem,
    AuthMode,
    LaunchClass,
    LaunchFacts,
    ProviderRequirements,
    config_file_problem,
    describe_launch,
    executable_requirements,
    install_root,
    interpreter_of,
    lookup_directories,
    sandbox_request,
)

from .conftest import GIT_IDENT


def _git(*args: str) -> str:
    out = subprocess.run(["git", *args], check=True, capture_output=True, text=True)
    return out.stdout.strip()


def _layout(tmp_path: Path) -> dict[str, Path]:
    """A checkout with an issue worktree in the default place, a fake home."""
    base = Path(os.path.realpath(tmp_path))
    repo = base / "repo"
    repo.mkdir()
    _git("init", "-q", "-b", "main", str(repo))
    _git("-C", str(repo), *GIT_IDENT, "commit", "-q", "--allow-empty", "-m", "initial")
    common = repo / ".git"
    worktree = common / "autoforge" / "worktrees" / "7"
    _git("-C", str(repo), "worktree", "add", "-q", "--detach", str(worktree), "HEAD")
    sandbox = common / "autoforge" / "sandbox" / "7"
    for name in ("objects", "overlay-work", "cache"):
        (sandbox / name).mkdir(parents=True)
    state = repo / ".autoforge"
    context = state / "read-context" / "run" / "analyze_execute-1-abc"
    context.mkdir(parents=True)
    home = base / "home"
    home.mkdir()
    config = base / "autoforge-child-1"
    (config / "gh").mkdir(parents=True)
    (config / "gitconfig").write_text("")
    return {
        "base": base,
        "repo": repo,
        "common": common,
        "worktree": worktree,
        "admin": common / "worktrees" / "7",
        "sandbox": sandbox,
        "state": state,
        "context": context,
        "home": home,
        "config": config,
    }


def _remote(paths: dict[str, Path], *, cache: bool = True) -> LaunchFacts:
    return LaunchFacts(
        launch_class=LaunchClass.REMOTE_AGENT,
        workspace=str(paths["worktree"]),
        home=str(paths["home"]),
        controller_env={},
        git_entry=str(paths["worktree"] / ".git"),
        git_dir=str(paths["admin"]),
        common_dir=str(paths["common"]),
        private_objects=str(paths["sandbox"] / "objects"),
        overlay_work=str(paths["sandbox"] / "overlay-work"),
        cache=str(paths["sandbox"] / "cache") if cache else "",
        read_context=str(paths["context"]),
        controller_config=str(paths["config"]),
        denied_roots=(
            str(paths["state"]),
            str(paths["repo"]),
            str(paths["common"] / "autoforge"),
        ),
    )


def _local(paths: dict[str, Path], kind: LaunchClass = LaunchClass.LOCAL_AGENT) -> LaunchFacts:
    git_dir = paths["common"]
    cache = git_dir / "autoforge" / "sandbox" / "local" / "cache"
    cache.mkdir(parents=True)
    state = git_dir / "autoforge" / "state"
    state.mkdir(parents=True)
    return LaunchFacts(
        launch_class=kind,
        workspace=str(paths["repo"]),
        home=str(paths["home"]),
        controller_env={},
        git_entry=str(git_dir),
        git_dir=str(git_dir),
        common_dir=str(git_dir),
        cache=str(cache),
        controller_config=str(paths["config"]),
        denied_roots=(str(state), str(git_dir / "autoforge"), str(paths["repo"] / ".autoforge")),
    )


def _mount(description: LaunchDescription, target: str):
    found = [m for m in description.mounts if m.target == target]
    assert found, f"nothing mounted at {target}"
    return found[0]


def test_a_remote_agent_gets_its_worktree_and_an_overlay_over_the_shared_store(tmp_path):
    paths = _layout(tmp_path)
    description = describe_launch(_remote(paths), None, {})
    common = str(paths["common"])
    worktree = _mount(description, str(paths["worktree"]))
    assert isinstance(worktree, Bind) and worktree.access is Access.WRITABLE
    assert _mount(description, str(paths["admin"])).access is Access.WRITABLE
    for pinned in (
        paths["worktree"] / ".git",
        paths["admin"] / "commondir",
        paths["admin"] / "gitdir",
    ):
        assert _mount(description, str(pinned)).access is Access.READ_ONLY
    assert _mount(description, common).access is Access.READ_ONLY
    for masked in ("autoforge", "worktrees"):
        assert isinstance(_mount(description, f"{common}/{masked}"), Mask)
    overlay = _mount(description, f"{common}/objects")
    assert isinstance(overlay, Overlay)
    assert overlay.upper == str(paths["sandbox"] / "objects")
    assert overlay.work == str(paths["sandbox"] / "overlay-work")
    assert _mount(description, str(paths["context"])).access is Access.READ_ONLY
    cache = _mount(description, str(paths["home"] / ".cache"))
    assert isinstance(cache, Bind) and cache.source == str(paths["sandbox"] / "cache")
    assert _mount(description, str(paths["config"])).access is Access.READ_ONLY
    # Nothing of the checkout, the state directory or the sandbox layers.
    for hidden in (paths["repo"], paths["state"], paths["sandbox"], paths["repo"] / "f.txt"):
        assert description.inside_path(str(hidden)) is None
    assert description.network is NetworkPosition.HOST


def test_a_review_launch_has_no_persistent_cache(tmp_path):
    paths = _layout(tmp_path)
    description = describe_launch(_remote(paths, cache=False), None, {})
    assert description.inside_path(str(paths["sandbox"] / "cache")) is None
    homes = [m for m in description.mounts if isinstance(m, PrivateTmpfs)]
    assert str(paths["home"]) in [m.target for m in homes]
    assert not any(m.target == str(paths["home"] / ".cache") for m in description.mounts)


def test_a_local_launch_sees_its_git_dir_read_only_and_its_state_masked(tmp_path):
    paths = _layout(tmp_path)
    description = describe_launch(_local(paths), None, {})
    root = _mount(description, str(paths["repo"]))
    assert isinstance(root, Bind) and root.access is Access.WRITABLE
    assert _mount(description, str(paths["common"])).access is Access.READ_ONLY
    for masked in ("autoforge", "worktrees"):
        assert isinstance(_mount(description, f"{paths['common']}/{masked}"), Mask)
    assert not any(isinstance(m, Overlay) for m in description.mounts)
    # The issue worktrees and the LOCAL state are behind the mask.
    assert description.inside_path(str(paths["worktree"])) is None
    assert description.inside_path(str(paths["common"] / "autoforge" / "state")) is None
    assert description.inside_path(str(paths["common"] / "HEAD")) is not None


def test_a_local_validation_command_may_run_without_a_network(tmp_path):
    paths = _layout(tmp_path)
    facts = _local(paths, LaunchClass.LOCAL_VALIDATION)
    facts = LaunchFacts(**{**facts.__dict__, "network": NetworkPosition.ISOLATED})
    assert describe_launch(facts, None, {}).network is NetworkPosition.ISOLATED


def test_an_agent_never_runs_without_a_network(tmp_path):
    paths = _layout(tmp_path)
    facts = LaunchFacts(**{**_remote(paths).__dict__, "network": NetworkPosition.ISOLATED})
    assert sandbox_request(facts).network is NetworkPosition.HOST


def test_an_export_command_sees_only_the_exported_tree(tmp_path):
    paths = _layout(tmp_path)
    tree = paths["base"] / "export" / "tree"
    tree.mkdir(parents=True)
    facts = LaunchFacts(
        launch_class=LaunchClass.EXPORT_COMMAND,
        workspace=str(tree),
        home=str(paths["home"]),
        controller_env={},
        cache=str(paths["sandbox"] / "cache"),
        controller_config=str(paths["config"]),
        denied_roots=(str(paths["state"]), str(paths["repo"])),
        network=NetworkPosition.ISOLATED,
    )
    needs = ProviderRequirements(install_roots=(str(paths["base"]),))
    description = describe_launch(facts, needs, {})
    assert _mount(description, str(tree)).access is Access.WRITABLE
    # No cache, no provider resource, nothing of the repository.
    for hidden in (paths["sandbox"] / "cache", paths["common"], paths["worktree"]):
        assert description.inside_path(str(hidden)) is None
    assert description.network is NetworkPosition.ISOLATED


def test_path_valued_names_pass_only_when_what_they_name_is_visible(tmp_path):
    paths = _layout(tmp_path)
    tools = paths["base"] / "tools" / "bin"
    tools.mkdir(parents=True)
    hidden = paths["base"] / "elsewhere"
    hidden.mkdir()
    env = {
        "PATH": f"{tools}:/usr/bin:{hidden}:relative:/bin",
        "GH_CONFIG_DIR": str(paths["config"] / "gh"),
        "GIT_CONFIG_GLOBAL": str(paths["config"] / "gitconfig"),
        "SSL_CERT_FILE": str(hidden / "ca.pem"),
        "CLAUDE_CONFIG_DIR": str(hidden),
        "OPENCODE_DB": ":memory:",
        "UNKNOWN_PATH": str(hidden),
    }
    needs = ProviderRequirements(
        install_roots=(str(tools),),
        path_dirs=(str(tools),),
        path_names=("CLAUDE_CONFIG_DIR", "OPENCODE_DB"),
    )
    description = describe_launch(_remote(paths), needs, env)
    assert dict(description.env_set)["PATH"] == f"{tools}:/usr/bin:/bin"
    assert {"SSL_CERT_FILE", "CLAUDE_CONFIG_DIR"} <= set(description.env_unset)
    for kept in ("GH_CONFIG_DIR", "GIT_CONFIG_GLOBAL", "OPENCODE_DB", "UNKNOWN_PATH"):
        assert kept not in description.env_unset and kept not in dict(description.env_set)
    # The compiler's own names follow the private home.
    assert dict(description.env_set)["HOME"] == str(paths["home"])
    assert "XDG_RUNTIME_DIR" in description.env_unset


def test_a_declared_writable_directory_on_an_operator_root_is_refused(tmp_path):
    paths = _layout(tmp_path)
    root = paths["home"] / ".pi" / "agent"
    root.mkdir(parents=True)
    needs = ProviderRequirements(
        auth=(AuthItem(AuthMode.DIR_RW, "Pi's agent directory", str(root)),),
        operator_config_roots=(str(root),),
    )
    with pytest.raises(SandboxLaunchRefusedError, match="D7.2") as caught:
        describe_launch(_remote(paths), needs, {})
    facts = caught.value.facts
    assert facts is not None and not facts.enforced and "D7.2" in facts.setup_failure


def test_a_keyring_login_is_refused(tmp_path):
    paths = _layout(tmp_path)
    needs = ProviderRequirements(auth=(AuthItem(AuthMode.KEYRING, "the Secret Service login"),))
    with pytest.raises(SandboxLaunchRefusedError, match="keyring"):
        describe_launch(_remote(paths), needs, {})


def test_a_declared_path_inside_a_denied_root_is_refused(tmp_path):
    paths = _layout(tmp_path)
    needs = ProviderRequirements(config=(str(paths["state"]),))
    with pytest.raises(SandboxLaunchRefusedError, match="denied root"):
        describe_launch(_remote(paths), needs, {})


def test_an_auth_directory_is_named_by_its_variable_inside(tmp_path):
    paths = _layout(tmp_path)
    agent_dir = paths["base"] / "pi-agent"
    agent_dir.mkdir()
    item = AuthItem(AuthMode.DIR_RW, "Pi", str(agent_dir), env_name="PI_CODING_AGENT_DIR")
    description = describe_launch(_remote(paths), ProviderRequirements(auth=(item,)), {})
    assert _mount(description, str(agent_dir)).access is Access.WRITABLE
    assert dict(description.env_set)["PI_CODING_AGENT_DIR"] == str(agent_dir)


def _linked(link: Path, target: Path) -> Path:
    """``link``, a symbolic link to the directory or file ``target``."""
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)
    return link


def _bound_at(description: LaunchDescription) -> set[str]:
    return {m.target for m in description.mounts if isinstance(m, Bind)}


@pytest.mark.parametrize("where", ["home", "elsewhere"])
def test_a_linked_auth_directory_is_named_where_it_is_projected(tmp_path, where):
    """A link to the dedicated directory: the compiler projects what it
    resolves to, so the variable names that, not the link, which is absent
    inside (in the private home, or where the root is empty) (D8.2)."""
    paths = _layout(tmp_path)
    real = paths["base"] / "pi-autoforge"
    real.mkdir()
    parent = paths["home"] if where == "home" else paths["base"] / "links"
    alias = _linked(parent / "pi-agent", real)
    env = {"PI_CODING_AGENT_DIR": str(alias)}
    needs = PiProvider().sandbox_requirements(_profile("pi"), ["pi"], env, str(paths["home"]))
    description = describe_launch(_remote(paths), needs, env)
    assert _mount(description, str(real)).access is Access.WRITABLE
    assert dict(description.env_set)["PI_CODING_AGENT_DIR"] == str(real)
    assert description.resolve_inside(str(alias)) != str(real)
    # Nothing more of the host is shown: not the link, not its directory.
    assert str(alias) not in _bound_at(description)
    assert description.inside_path(str(parent)) is None


def test_linked_provider_paths_are_named_where_they_are_projected(tmp_path):
    """``CLAUDE_CONFIG_DIR`` (an auth item's variable), ``OPENCODE_DB`` and
    ``OPENCODE_CONFIG_DIR`` (declared path names) through links in the home."""
    paths = _layout(tmp_path)
    home = paths["home"]
    claude_real = paths["base"] / "claude-autoforge"
    db_real = paths["base"] / "opencode-autoforge"
    config_real = paths["base"] / "autoforge-opencode-1"
    for directory in (claude_real, db_real, config_real):
        directory.mkdir()
    claude_env = {"CLAUDE_CONFIG_DIR": str(_linked(home / ".claude-autoforge", claude_real))}
    needs = ClaudeCodeProvider().sandbox_requirements(
        _profile("claude"), ["claude"], claude_env, str(home)
    )
    description = describe_launch(_remote(paths), needs, claude_env)
    assert dict(description.env_set)["CLAUDE_CONFIG_DIR"] == str(claude_real)
    opencode_env = {
        "OPENCODE_DB": str(_linked(home / ".opencode-autoforge", db_real) / "af.db"),
        "OPENCODE_CONFIG_DIR": str(_linked(home / "opencode-config", config_real)),
    }
    needs = OpenCodeProvider().sandbox_requirements(
        _profile("opencode"), ["opencode"], opencode_env, str(home)
    )
    description = describe_launch(_remote(paths), needs, opencode_env)
    env_set = dict(description.env_set)
    assert env_set["OPENCODE_DB"] == str(db_real / "af.db")
    assert env_set["OPENCODE_CONFIG_DIR"] == str(config_real)
    assert _mount(description, str(db_real)).access is Access.WRITABLE
    for alias in (home / ".opencode-autoforge", home / "opencode-config"):
        assert str(alias) not in _bound_at(description)


def test_a_path_in_a_relocated_projection_is_named_where_it_appears(tmp_path):
    """The cache is projected at ``~/.cache``: a value or a ``PATH`` entry
    naming its host directory is visible there, and is set to that."""
    paths = _layout(tmp_path)
    cache = paths["sandbox"] / "cache"
    (cache / "bin").mkdir()
    env = {"TOOL_CACHE": str(cache / "tool"), "PATH": f"{cache / 'bin'}:/usr/bin"}
    needs = ProviderRequirements(path_names=("TOOL_CACHE",))
    description = describe_launch(_remote(paths), needs, env)
    env_set = dict(description.env_set)
    inside = paths["home"] / ".cache"
    assert env_set["TOOL_CACHE"] == str(inside / "tool")
    assert env_set["PATH"] == f"{inside / 'bin'}:/usr/bin"


def test_linked_path_entries_are_named_where_they_are_projected(tmp_path):
    """A ``PATH`` entry through a link the sandbox does not recreate is the
    projected directory; one through a link it recreates keeps its spelling."""
    paths = _layout(tmp_path)
    tools = paths["base"] / "tools" / "bin"
    tools.mkdir(parents=True)
    alias = _linked(paths["home"] / ".local" / "bin", tools)
    needs = ProviderRequirements(install_roots=(str(tools),))
    env = {"PATH": f"{alias}:/usr/bin:/bin:{tools}"}
    description = describe_launch(_remote(paths), needs, env)
    # ``/bin`` stays as spelled: where it is a link, the sandbox recreates it.
    assert dict(description.env_set)["PATH"] == f"{tools}:/usr/bin:/bin"


def test_claudes_linked_default_files_are_projected_where_it_looks(tmp_path):
    """``~/.claude/.credentials.json`` and ``~/.claude/settings.json`` as
    links into a dotfiles directory: each appears at its own path in the
    private home, read-only, and the dotfiles directory is not shown."""
    paths = _layout(tmp_path)
    home = paths["home"]
    dotfiles = home / "dotfiles"
    dotfiles.mkdir()
    (dotfiles / "claude-login.json").write_text("{}")
    (dotfiles / "claude-settings.json").write_text("{}")
    claude = home / ".claude"
    login = _linked(claude / ".credentials.json", dotfiles / "claude-login.json")
    settings = _linked(claude / "settings.json", dotfiles / "claude-settings.json")
    needs = ClaudeCodeProvider().sandbox_requirements(_profile("claude"), ["claude"], {}, str(home))
    description = describe_launch(_remote(paths), needs, {})
    for path, real in ((login, "claude-login.json"), (settings, "claude-settings.json")):
        mount = _mount(description, str(path))
        assert isinstance(mount, Bind) and mount.access is Access.READ_ONLY
        assert mount.source == str(dotfiles / real) and not mount.is_dir
        assert description.resolve_inside(str(path)) == str(path)
    assert description.resolve_inside(str(dotfiles)) == str(dotfiles)  # absent: kept as spelled
    assert str(dotfiles) not in _bound_at(description)
    assert description.inside_path(str(dotfiles)) is None


# ------------------------------------------------- executables and install roots


def test_install_roots_are_the_package_or_the_directory_never_the_home(tmp_path):
    home = "/home/op"
    pkg = "/opt/n/lib/node_modules/@earendil-works/pi-coding-agent/dist/cli.js"
    assert install_root(pkg, home) == "/opt/n/lib/node_modules/@earendil-works/pi-coding-agent"
    assert install_root("/opt/n/lib/node_modules/tool/bin/t.js", home) == (
        "/opt/n/lib/node_modules/tool"
    )
    assert install_root("/home/op/.local/share/claude/versions/2.1.0", home) == (
        "/home/op/.local/share/claude/versions"
    )
    # A file directly in the home (or above it) is projected alone.
    assert install_root("/home/op/claude", home) == "/home/op/claude"
    assert install_root("/claude", home) == "/claude"


def test_an_interpreter_installed_under_a_prefix_brings_its_library(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    home = base / "home"
    python = base / "home" / ".local" / "share" / "uv" / "python" / "cpython-3.14" / "bin"
    python.mkdir(parents=True)
    (python.parent / "lib").mkdir()
    assert install_root(str(python / "python3.14"), str(home)) == str(python.parent)
    # Never the home's own prefixes, and not a prefix without a library.
    for prefix in (home, home / ".local", home / ".cargo"):
        (prefix / "bin").mkdir(parents=True, exist_ok=True)
        if prefix != home / ".cargo":
            (prefix / "lib").mkdir(exist_ok=True)
        root = install_root(str(prefix / "bin" / "tool"), str(home))
        assert root == str(prefix / "bin"), prefix


@pytest.mark.skipif(not os.access("/usr/bin/env", os.X_OK), reason="no /usr/bin/env on this host")
def test_a_script_brings_its_interpreter(tmp_path):
    base = Path(os.path.realpath(tmp_path))
    interp_dir = base / "runtime" / "bin"
    interp_dir.mkdir(parents=True)
    interp = interp_dir / "node"
    interp.write_text("#!/bin/sh\n")
    interp.chmod(0o755)
    tool_dir = base / "tool"
    tool_dir.mkdir()
    tool = tool_dir / "agent"
    tool.write_text("#!/usr/bin/env -S node --flag\nconsole.log(1)\n")
    tool.chmod(0o755)
    search = f"{tool_dir}:{interp_dir}:/usr/bin"
    assert interpreter_of(str(tool), search) == str(interp)
    executable, roots, path_dirs, spelled = executable_requirements(
        "agent", home=str(base / "home"), search_path=search
    )
    assert executable == str(tool)
    # The kernel runs ``env`` as written; it looks the name up on PATH,
    # whose first entry is the name's directory.
    env_root = install_root(os.path.realpath("/usr/bin/env"), str(base / "home"))
    assert roots == (str(tool_dir), env_root, str(interp_dir))
    assert path_dirs == (str(interp_dir),)
    assert spelled == ("/usr/bin/env",)
    missing = executable_requirements("no-such-agent", home="/h", search_path=search)
    assert missing == ("", (), (), ())


def test_an_absolute_shebang_through_a_link_is_reached_as_written(tmp_path):
    """#225: ``#!<base>/alias/bin/python3``, a link to the real interpreter
    in a directory no root projects. The interpreter's root is projected as
    before, and the line, as written, is a spelled path: the description
    recreates that one link, not ``alias/bin``, which is neither a root nor
    a ``PATH`` entry inside, and the kernel's lookup of the line inside
    reaches the interpreter."""
    paths = _layout(tmp_path)
    base, home = paths["base"], str(paths["home"])
    interp = base / "runtime" / "bin" / "python3.14"
    interp.parent.mkdir(parents=True)
    (base / "runtime" / "lib").mkdir()
    interp.write_text("#!/bin/sh\n")
    interp.chmod(0o755)
    shebang = _linked(base / "alias" / "bin" / "python3", interp)
    (base / "alias" / "bin" / "pip").write_text("")
    tool = base / "tool" / "agent"
    tool.parent.mkdir()
    tool.write_text(f"#!{shebang}\nprint(1)\n")
    tool.chmod(0o755)
    search = f"{tool.parent}:{shebang.parent}:/usr/bin"
    assert interpreter_of(str(tool), search) == str(interp)
    assert executable_requirements("agent", home=home, search_path=search) == (
        str(tool),
        (str(tool.parent), str(base / "runtime")),
        (str(interp.parent),),
        (str(shebang),),
    )
    env = {"PATH": search}
    needs = ClaudeCodeProvider().sandbox_requirements(_profile("claude"), ["agent"], env, home)
    assert needs.spelled_paths == (str(shebang),)
    description = describe_launch(_remote(paths), needs, env)
    alias = [m for m in description.mounts if m.target.startswith(f"{base / 'alias'}/")]
    assert alias == [Link(str(shebang), str(interp))]
    assert description.resolve_inside(str(shebang)) == str(interp)
    assert description.inside_path(str(shebang.parent / "pip")) is None
    assert dict(description.env_set)["PATH"] == f"{interp.parent}:{tool.parent}:/usr/bin"


@pytest.mark.parametrize("where", ["system", "own"])
def test_an_absolute_env_through_a_link_is_reached_as_written(tmp_path, where):
    """#225 R1-F1: ``#!<base>/alias/bin/env node``, a link in a directory no
    root projects to the system's ``/usr/bin/env`` or to an ``env`` in its
    own prefix. The kernel runs that ``env`` as written, so the line is a
    spelled path and its program's root is projected, as an interpreter's
    is; ``env`` still finds ``node`` on ``PATH``, whose first entry is the
    directory of ``node``. Only the one link is recreated, not ``alias/bin``."""
    paths = _layout(tmp_path)
    base, home = paths["base"], str(paths["home"])
    if where == "system":
        if not os.access("/usr/bin/env", os.X_OK):
            pytest.skip("no /usr/bin/env on this host")
        target = Path("/usr/bin/env")
    else:
        target = base / "coreutils" / "bin" / "env"
        target.parent.mkdir(parents=True)
        (base / "coreutils" / "lib").mkdir()
        target.write_text("#!/bin/sh\n")
        target.chmod(0o755)
    real_env = os.path.realpath(target)
    shebang = _linked(base / "alias" / "bin" / "env", target)
    (base / "alias" / "bin" / "pip").write_text("")
    interp = base / "runtime" / "bin" / "node"
    interp.parent.mkdir(parents=True)
    (base / "runtime" / "lib").mkdir()
    interp.write_text("#!/bin/sh\n")
    interp.chmod(0o755)
    tool = base / "tool" / "agent"
    tool.parent.mkdir()
    tool.write_text(f"#!{shebang} node\nconsole.log(1)\n")
    tool.chmod(0o755)
    search = f"{tool.parent}:{interp.parent}:/usr/bin"
    assert interpreter_of(str(tool), search) == str(interp)
    assert executable_requirements("agent", home=home, search_path=search) == (
        str(tool),
        (str(tool.parent), install_root(real_env, home), str(base / "runtime")),
        (str(interp.parent),),
        (str(shebang),),
    )
    env = {"PATH": search}
    needs = ClaudeCodeProvider().sandbox_requirements(_profile("claude"), ["agent"], env, home)
    assert needs.spelled_paths == (str(shebang),)
    description = describe_launch(_remote(paths), needs, env)
    alias = [m for m in description.mounts if m.target.startswith(f"{base / 'alias'}/")]
    assert alias == [Link(str(shebang), str(target))]
    assert description.resolve_inside(str(shebang)) == real_env
    assert description.inside_path(real_env) == real_env
    assert description.inside_path(str(shebang.parent / "pip")) is None
    assert dict(description.env_set)["PATH"] == f"{interp.parent}:{tool.parent}:/usr/bin"


# ------------------------------------------------------------- the declarations


def _profile(provider: str) -> ProfileConfig:
    return ProfileConfig(name="analyze_execute", provider=provider, model="m/x", effort="high")


def test_claude_declares_its_login_settings_and_shell_file(tmp_path):
    home = tmp_path / "home"
    claude = home / ".claude"
    claude.mkdir(parents=True)
    (claude / ".credentials.json").write_text("{}")
    (claude / "settings.json").write_text("{}")
    env_file = tmp_path / "env.sh"
    env_file.write_text("")
    needs = ClaudeCodeProvider().sandbox_requirements(
        _profile("claude"),
        ["claude"],
        {"CLAUDE_ENV_FILE": str(env_file), "ANTHROPIC_API_KEY": "x", "PATH": ""},
        str(home),
    )
    modes = {(i.mode, i.source or i.label) for i in needs.auth}
    assert (AuthMode.FILE_RO, str(claude / ".credentials.json")) in modes
    assert (AuthMode.ENV, "ANTHROPIC_API_KEY") in modes
    # Where the CLI looks for them, in the private home.
    login = next(i for i in needs.auth if i.mode is AuthMode.FILE_RO)
    assert login.target == str(claude / ".credentials.json")
    assert needs.home_config == (str(claude / "settings.json"),)
    assert needs.config == (str(env_file),)
    assert str(claude) in needs.operator_config_roots
    assert "CLAUDE_CONFIG_DIR" in needs.path_names


def test_claude_with_its_own_configuration_directory_gets_it_writable(tmp_path):
    home = tmp_path / "home"
    dedicated = tmp_path / "claude-autoforge"
    dedicated.mkdir()
    needs = ClaudeCodeProvider().sandbox_requirements(
        _profile("claude"), ["claude"], {"CLAUDE_CONFIG_DIR": str(dedicated)}, str(home)
    )
    assert [(i.mode, i.source, i.env_name) for i in needs.auth] == [
        (AuthMode.DIR_RW, str(dedicated), "CLAUDE_CONFIG_DIR")
    ]


def test_claude_pointed_at_the_operators_own_directory_is_refused(tmp_path):
    paths = _layout(tmp_path)
    own = paths["home"] / ".claude"
    own.mkdir()
    needs = ClaudeCodeProvider().sandbox_requirements(
        _profile("claude"), ["claude"], {"CLAUDE_CONFIG_DIR": str(own)}, str(paths["home"])
    )
    with pytest.raises(SandboxLaunchRefusedError, match="operator configuration root"):
        describe_launch(_remote(paths), needs, {})


def test_opencode_declares_a_database_of_its_own_and_its_config_directory(tmp_path):
    home = tmp_path / "home"
    db_dir = tmp_path / "opencode-autoforge"
    db_dir.mkdir()
    config = tmp_path / "autoforge-opencode-1"
    config.mkdir()
    env = {
        "OPENCODE_DB": str(db_dir / "af.db"),
        "OPENCODE_CONFIG_DIR": str(config),
        "OPENAI_API_KEY": "x",
    }
    provider = OpenCodeProvider()
    needs = provider.sandbox_requirements(_profile("opencode"), ["opencode"], env, str(home))
    assert (AuthMode.DIR_RW, str(db_dir)) in {(i.mode, i.source) for i in needs.auth}
    assert (AuthMode.ENV, "OPENAI_API_KEY") in {(i.mode, i.label) for i in needs.auth}
    assert needs.state == (str(config),)
    assert str(home / ".config" / "opencode") in needs.operator_config_roots


def test_pi_declares_its_agent_directory_and_never_the_operators(tmp_path):
    paths = _layout(tmp_path)
    dedicated = paths["base"] / "pi-autoforge"
    dedicated.mkdir()
    provider = PiProvider()
    needs = provider.sandbox_requirements(
        _profile("pi"), ["pi"], {"PI_CODING_AGENT_DIR": str(dedicated)}, str(paths["home"])
    )
    assert [(i.mode, i.env_name) for i in needs.auth] == [(AuthMode.DIR_RW, "PI_CODING_AGENT_DIR")]
    assert needs.operator_config_roots == (str(paths["home"] / ".pi" / "agent"),)
    describe_launch(_remote(paths), needs, {})
    own = paths["home"] / ".pi" / "agent"
    own.mkdir(parents=True)
    needs = provider.sandbox_requirements(
        _profile("pi"), ["pi"], {"PI_CODING_AGENT_DIR": str(own)}, str(paths["home"])
    )
    with pytest.raises(SandboxLaunchRefusedError, match="D7.2"):
        describe_launch(_remote(paths), needs, {})


# -- where a configuration file may lie (#280) -------------------------------------


def _tree(tmp_path) -> tuple[Path, Path, Path]:
    """A resolved base, a written root inside it, and a directory outside the root."""
    base = Path(os.path.realpath(tmp_path))
    root = base / "repo"
    (root / "conf").mkdir(parents=True)
    outside = base / "operator"
    outside.mkdir()
    return base, root, outside


def test_the_lookup_walk_lists_each_directory_a_name_is_looked_up_in(tmp_path):
    base, root, _ = _tree(tmp_path)
    (root / "conf" / "a.yaml").write_text("version: 1\n")
    walked = lookup_directories(str(root / "conf" / "a.yaml"))
    assert walked[0] == "/"
    assert walked[-2:] == (str(root), str(root / "conf"))
    assert lookup_directories(str(root / "conf" / "missing" / "a.yaml"))[-1] == str(root / "conf")


def test_the_lookup_walk_follows_links_as_the_kernel_does(tmp_path):
    base, root, outside = _tree(tmp_path)
    (root / "conf" / "a.yaml").write_text("version: 1\n")
    (outside / "absolute").symlink_to(root / "conf")
    (outside / "relative").symlink_to(os.path.join("..", "repo", "conf"))
    (outside / "dotdot").symlink_to(os.path.join("..", "repo", "conf", "..", "conf"))
    for link in ("absolute", "relative", "dotdot"):
        walked = lookup_directories(str(outside / link / "a.yaml"))
        assert str(outside) in walked and walked[-1] == str(root / "conf"), link


def test_the_lookup_walk_stops_at_a_link_loop(tmp_path):
    base, _, outside = _tree(tmp_path)
    (outside / "a").symlink_to(outside / "b")
    (outside / "b").symlink_to(outside / "a")
    assert lookup_directories(str(outside / "a" / "x.yaml"))[-1] == str(outside)


def test_a_configuration_file_in_a_written_root_is_refused(tmp_path):
    _, root, _ = _tree(tmp_path)
    cfg = root / "conf" / "autoforge.yaml"
    cfg.write_text("version: 1\n")
    problem = config_file_problem(str(cfg), (str(root),), "required")
    assert problem.startswith(f"the configuration file {cfg} lies in {root}, a tree that ")
    assert "(sandbox.mode is required)" in problem
    assert "a sandboxed agent could rewrite it" in problem
    assert "Move it outside every working tree of this repository" in problem
    assert "set sandbox.mode: disabled" in problem


def test_a_configuration_file_outside_every_written_root_passes(tmp_path):
    base, root, outside = _tree(tmp_path)
    cfg = outside / "autoforge.yaml"
    cfg.write_text("version: 1\n")
    assert config_file_problem(str(cfg), (str(root),), "required") == ""
    # A file beside the root, in its parent, is not inside it.
    beside = base / "autoforge.yaml"
    beside.write_text("version: 1\n")
    assert config_file_problem(str(beside), (str(root),), "preferred") == ""
    # Nor is a sibling whose name only begins like the root's.
    sibling = base / "repo-config"
    sibling.mkdir()
    (sibling / "a.yaml").write_text("version: 1\n")
    assert config_file_problem(str(sibling / "a.yaml"), (str(root),), "required") == ""


def test_a_link_from_outside_into_a_written_root_is_refused(tmp_path):
    _, root, outside = _tree(tmp_path)
    (root / "conf" / "autoforge.yaml").write_text("version: 1\n")
    (outside / "autoforge.yaml").symlink_to(root / "conf" / "autoforge.yaml")
    (outside / "dir").symlink_to(root / "conf")
    for spelled in (outside / "autoforge.yaml", outside / "dir" / "autoforge.yaml"):
        problem = config_file_problem(str(spelled), (str(root),), "required")
        assert f"the configuration file {spelled} is reached through " in problem
        assert f", in {root}, a tree that " in problem


def test_a_link_inside_a_written_root_to_a_file_outside_is_refused(tmp_path):
    """The agent replaces the link, not the file it names."""
    _, root, outside = _tree(tmp_path)
    (outside / "autoforge.yaml").write_text("version: 1\n")
    (root / "conf" / "autoforge.yaml").symlink_to(outside / "autoforge.yaml")
    problem = config_file_problem(str(root / "conf" / "autoforge.yaml"), (str(root),), "required")
    assert f"is reached through {root / 'conf'}, in {root}" in problem


def test_a_written_root_spelled_through_a_link_is_compared_resolved(tmp_path):
    base, root, _ = _tree(tmp_path)
    cfg = root / "conf" / "autoforge.yaml"
    cfg.write_text("version: 1\n")
    (base / "alias").symlink_to(root)
    problem = config_file_problem(str(cfg), (str(base / "alias"),), "required")
    assert f"lies in {base / 'alias'}, a tree that " in problem


def test_a_configuration_file_with_a_second_hard_link_is_refused(tmp_path):
    """ADR 0005 D4.4: a hard link shows nowhere in the walk."""
    _, root, outside = _tree(tmp_path)
    cfg = outside / "autoforge.yaml"
    cfg.write_text("version: 1\n")
    os.link(cfg, root / "conf" / "linked.yaml")
    problem = config_file_problem(str(cfg), (str(root),), "preferred")
    assert f"the configuration file {cfg} has 2 hard links" in problem
    assert "Copy it to a file of its own outside every working tree" in problem


def test_a_missing_configuration_file_is_judged_by_where_it_would_be(tmp_path):
    _, root, outside = _tree(tmp_path)
    assert "lies in" in config_file_problem(str(root / "conf" / "x.yaml"), (str(root),), "required")
    assert config_file_problem(str(outside / "x.yaml"), (str(root),), "required") == ""
