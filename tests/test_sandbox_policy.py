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

from autoforge.config import ProfileConfig, default_config
from autoforge.engine import config_file_placement_problem
from autoforge.errors import SandboxLaunchRefusedError
from autoforge.providers import ClaudeCodeProvider, OpenCodeProvider, PiProvider
from autoforge.sandbox import (
    Access,
    Bind,
    LaunchDescription,
    Link,
    Mask,
    NetworkPosition,
    Origin,
    Overlay,
    PrivateTmpfs,
)
from autoforge.sandbox_policy import (
    AuthItem,
    AuthMode,
    LaunchClass,
    LaunchFacts,
    ProviderRequirements,
    absolute_spelling,
    config_file_problem,
    declared_writable,
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


@pytest.mark.parametrize("access", [Access.READ_ONLY, Access.WRITABLE])
def test_a_provider_path_under_the_relocated_cache_is_refused_where_it_appears(tmp_path, access):
    """The cache projected at ``~/.cache`` hides the operator's own, so a
    provider path there (a ``TMPDIR`` under ``~/.cache``, #301) is refused
    (C6), naming the cache directory and where it appears. Without the
    persistent cache the path lies in the private home and is projected."""
    paths = _layout(tmp_path)
    under = paths["home"] / ".cache" / "tmp" / "pi"
    under.mkdir(parents=True)
    needs = (
        ProviderRequirements(install_roots=(str(under),))
        if access is Access.READ_ONLY
        else ProviderRequirements(
            auth=(AuthItem(AuthMode.DIR_RW, "Pi", str(under), env_name="PI_CODING_AGENT_DIR"),)
        )
    )
    with pytest.raises(SandboxLaunchRefusedError, match="C6 overlaps") as caught:
        describe_launch(_remote(paths), needs, {})
    cache, inside = paths["sandbox"] / "cache", paths["home"] / ".cache"
    assert f"refuses {under}: C6 overlaps:" in str(caught.value)
    assert f"overlaps the projection of {cache} at {inside}" in str(caught.value)
    description = describe_launch(_remote(paths, cache=False), needs, {})
    assert _mount(description, str(under)).access is access


def test_a_provider_path_in_a_projection_at_its_own_path_is_refused_naming_it_once(tmp_path):
    """C6: a writable declared directory inside the launch's read-only
    configuration, projected where it lies on the host, names that path."""
    paths = _layout(tmp_path)
    inner = paths["config"] / "gh"
    needs = ProviderRequirements(state=(str(inner),))
    with pytest.raises(SandboxLaunchRefusedError, match="C6 overlaps") as caught:
        describe_launch(_remote(paths), needs, {})
    assert str(caught.value).endswith(
        f"{inner} (provider) overlaps the projection {paths['config']}"
    )


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


# A ``..`` is taken where the walk is, never collapsed first (#280, R2-F1): with
# ``operator/alias`` a link to ``repo/conf``, ``operator/alias/../a.yaml`` opens
# ``repo/a.yaml``, and ``os.path.abspath`` spells it ``operator/a.yaml``.


def test_the_absolute_spelling_keeps_every_dotdot_and_joins_the_working_directory(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    here = os.getcwd()
    assert absolute_spelling("alias/../a.yaml") == f"{here}/alias/../a.yaml"
    assert absolute_spelling("../x/./a.yaml") == f"{here}/../x/a.yaml"
    assert absolute_spelling("/operator//alias/../a.yaml") == "/operator/alias/../a.yaml"
    assert absolute_spelling(Path("/operator/a.yaml")) == "/operator/a.yaml"


def _dotdot_tree(tmp_path) -> tuple[Path, Path, Path]:
    """``operator/{absolute,relative}`` link to ``repo/conf``; ``a.yaml`` in both parents."""
    base, root, outside = _tree(tmp_path)
    for directory in (root, outside):
        (directory / "a.yaml").write_text("version: 1\n")
    (outside / "absolute").symlink_to(root / "conf")
    (outside / "relative").symlink_to(os.path.join("..", "repo", "conf"))
    return base, root, outside


@pytest.mark.parametrize("link", ["absolute", "relative"])
def test_a_dotdot_after_a_link_into_a_written_root_is_refused(tmp_path, link):
    _, root, outside = _dotdot_tree(tmp_path)
    spelled = f"{outside}/{link}/../a.yaml"
    assert os.path.realpath(spelled) == str(root / "a.yaml")  # what the loader opens
    problem = config_file_problem(spelled, (str(root),), "required")
    assert problem.startswith(f"the configuration file {spelled} is reached through {root}, ")
    assert f", in {root}, a tree that " in problem
    assert str(root) in lookup_directories(spelled)
    # The lexically collapsed spelling is the operator's file, which passes.
    assert config_file_problem(os.path.abspath(spelled), (str(root),), "required") == ""


@pytest.mark.parametrize("link", ["absolute", "relative"])
def test_a_relative_path_with_a_dotdot_after_a_link_is_walked_from_the_working_directory(
    tmp_path, monkeypatch, link
):
    _, root, outside = _dotdot_tree(tmp_path)
    monkeypatch.chdir(outside)
    problem = config_file_problem(f"{link}/../a.yaml", (str(root),), "preferred")
    assert problem.startswith(
        f"the configuration file {os.getcwd()}/{link}/../a.yaml is reached through {root}, "
    )
    monkeypatch.chdir(root)  # from inside the root, a .. that leaves it looks up nothing there
    assert config_file_problem("../operator/a.yaml", (str(root),), "required") == ""
    assert "reached through" in config_file_problem(
        f"../operator/{link}/../a.yaml", (str(root),), "required"
    )


def test_a_path_that_looks_up_a_name_in_a_written_root_and_leaves_it_is_refused(
    tmp_path, monkeypatch
):
    """The file opened is outside, but the agent can replace ``conf``, which the
    lookup goes through, with a link that keeps the ``..`` inside the root."""
    base, root, outside = _dotdot_tree(tmp_path)
    for spelled in (f"{root}/conf/../../operator/a.yaml", f"{root}/conf/../conf/../a.yaml"):
        problem = config_file_problem(spelled, (str(root),), "required")
        assert problem.startswith(f"the configuration file {spelled} is reached through "), spelled
        assert f", in {root}, a tree that " in problem
    monkeypatch.chdir(root)
    problem = config_file_problem("conf/../../operator/a.yaml", (str(root),), "required")
    assert f"is reached through {root}, in {root}" in problem
    # A directory the walk passes outside every root, and back out of, is no reach.
    (outside / "sub").mkdir()
    assert config_file_problem(f"{outside}/sub/../a.yaml", (str(root),), "required") == ""


def test_a_dotdot_after_a_link_that_stays_outside_every_written_root_passes(tmp_path):
    base, root, outside = _tree(tmp_path)
    elsewhere = base / "elsewhere"
    (elsewhere / "conf").mkdir(parents=True)
    (elsewhere / "a.yaml").write_text("version: 1\n")
    (outside / "alias").symlink_to(elsewhere / "conf")
    assert config_file_problem(f"{outside}/alias/../a.yaml", (str(root),), "required") == ""


def test_the_hard_links_counted_are_the_file_opened(tmp_path):
    """The lexical twin has one link; the file the path opens has two."""
    base, root, outside = _tree(tmp_path)
    elsewhere = base / "elsewhere"
    (elsewhere / "conf").mkdir(parents=True)
    (elsewhere / "a.yaml").write_text("version: 1\n")
    os.link(elsewhere / "a.yaml", root / "conf" / "linked.yaml")
    (outside / "a.yaml").write_text("version: 1\n")
    (outside / "alias").symlink_to(elsewhere / "conf")
    spelled = f"{outside}/alias/../a.yaml"
    assert f"the configuration file {spelled} has 2 hard links" in config_file_problem(
        spelled, (str(root),), "required"
    )


@pytest.mark.skipif(not os.path.isdir("/proc/self/fd"), reason="needs Linux's /proc")
def test_a_file_the_lookup_walk_does_not_end_at_is_refused(tmp_path):
    """A /proc magic link reads as the path of a deleted file, which the walk
    cannot follow, while ``open`` reaches the file anyway: the walk would
    have examined something else than the file opened."""
    _, root, outside = _tree(tmp_path)
    cfg = outside / "a.yaml"
    cfg.write_text("version: 1\n")
    with open(cfg, encoding="utf-8") as handle:
        cfg.unlink()
        spelled = f"/proc/self/fd/{handle.fileno()}"
        problem = config_file_problem(spelled, (str(root),), "required")
    assert problem.startswith(
        f"the configuration file {spelled} opens another file than its lookup walk was followed to"
    )
    assert "(sandbox.mode is required; #280)" in problem


# -- the directories a provider's own launches write (#280, R3-F1) ------------------
# A dedicated ``CLAUDE_CONFIG_DIR``, ``PI_CODING_AGENT_DIR`` or OpenCode database
# directory is projected writable into every launch of its provider and outlives
# it: a configuration file there is one an agent can rewrite.

_DECLARED_WHAT = (
    "the Claude Code configuration directory ($CLAUDE_CONFIG_DIR), which every sandboxed "
    "launch of claude (profile fix) writes"
)


@pytest.mark.parametrize("cache", [True, False], ids=["agent", "review"])
def test_a_remote_agent_declaration_makes_its_state_and_dir_rw_items_writable_only(tmp_path, cache):
    """ADR 0009 A.2.3 F (#386): the REMOTE replacement of
    test_sandbox_policy.py::test_a_declaration_makes_its_state_and_dir_rw_items_writable_and_nothing_else,
    deleted with LOCAL (#388).

    On a REMOTE agent launch, with its cache and without it (a REVIEW):
    what :func:`declared_writable` names is what
    :func:`sandbox_request` projects read-write from the declaration, never
    a read-only file, an ``env`` item or a keyring login.
    """
    needs = ProviderRequirements(
        config=("/opt/tool/conf",),
        home_config=("/home/u/.tool/settings.json",),
        state=("/srv/tool-state",),
        auth=(
            AuthItem(AuthMode.ENV, "TOOL_API_KEY"),
            AuthItem(AuthMode.FILE_RO, "the tool login", "/home/u/.tool/login.json"),
            AuthItem(AuthMode.DIR_RW, "the tool directory ($TOOL_DIR)", "/srv/tool"),
            AuthItem(AuthMode.KEYRING, "the tool keyring login", "tool"),
        ),
    )
    assert declared_writable(needs) == (
        ("/srv/tool-state", "a state directory its provider declares (/srv/tool-state)"),
        ("/srv/tool", "the tool directory ($TOOL_DIR)"),
    )
    request = sandbox_request(_remote(_layout(tmp_path), cache=cache), needs)
    projected = [
        p.source
        for p in request.projections
        if p.origin is Origin.DECLARED and p.access is Access.WRITABLE
    ]
    assert projected == [path for path, _ in declared_writable(needs)]


def test_each_provider_names_the_directories_its_launches_write(tmp_path, monkeypatch):
    """Each adapter's own declaration, over the environment a launch
    inherits: ``CLAUDE_CONFIG_DIR``, ``PI_CODING_AGENT_DIR`` and the directory
    of ``OPENCODE_DB``. An operator's ``OPENCODE_CONFIG_DIR`` is not one:
    every OpenCode launch replaces it with an empty directory of its own."""
    base = Path(os.path.realpath(tmp_path))
    home = str(base / "home")
    allowlist = default_config().execution.environment_names()

    def written(provider) -> tuple[tuple[str, str], ...]:
        profile = ProfileConfig(name="p", provider=provider.name)
        return provider.written_directories(profile, allowlist, home)

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", f"{base}/claude")
    monkeypatch.setenv("PI_CODING_AGENT_DIR", f"{base}/pi")
    monkeypatch.setenv("OPENCODE_DB", f"{base}/opencode/af.db")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", f"{base}/opencode-config")
    assert written(ClaudeCodeProvider()) == (
        (f"{base}/claude", "the Claude Code configuration directory ($CLAUDE_CONFIG_DIR)"),
    )
    assert written(PiProvider()) == ((f"{base}/pi", "Pi's agent directory ($PI_CODING_AGENT_DIR)"),)
    assert written(OpenCodeProvider()) == (
        (f"{base}/opencode", "the OpenCode database directory ($OPENCODE_DB)"),
    )
    for name in ("CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR", "OPENCODE_DB"):
        monkeypatch.delenv(name)
    for provider in (ClaudeCodeProvider(), PiProvider(), OpenCodeProvider()):
        assert written(provider) == (), provider.name


def test_a_configuration_file_in_a_providers_writable_directory_is_refused(tmp_path):
    base, root, outside = _tree(tmp_path)
    claude = base / "claude-autoforge"
    (claude / "conf").mkdir(parents=True)
    declared = {str(claude): _DECLARED_WHAT}
    for cfg in (claude / "autoforge.yaml", claude / "conf" / "autoforge.yaml"):
        cfg.write_text("version: 1\n")
        problem = config_file_problem(str(cfg), (str(root),), "required", declared)
        assert problem.startswith(
            f"the configuration file {cfg} lies in {claude}, {_DECLARED_WHAT} "
            "(sandbox.mode is required): a sandboxed agent could rewrite it"
        )
        assert "Move it outside that directory and every working tree" in problem
        assert "give the provider another directory, or set sandbox.mode: disabled" in problem
    assert "lies in" in config_file_problem(str(claude / "x.yaml"), (), "preferred", declared)


def test_a_path_through_a_providers_writable_directory_is_refused(tmp_path):
    """The agent replaces what the lookup passes in the directory: a link to
    a file outside, a link into it, a name a ``..`` leaves again."""
    base, root, outside = _tree(tmp_path)
    claude = base / "claude-autoforge"
    (claude / "conf").mkdir(parents=True)
    (claude / "autoforge.yaml").write_text("version: 1\n")
    (outside / "a.yaml").write_text("version: 1\n")
    (claude / "linked.yaml").symlink_to(outside / "a.yaml")
    (outside / "alias").symlink_to(claude / "conf")
    declared = {str(claude): _DECLARED_WHAT}
    for spelled in (
        str(claude / "linked.yaml"),
        f"{outside}/alias/../autoforge.yaml",
        f"{claude}/conf/../../operator/a.yaml",
    ):
        problem = config_file_problem(spelled, (str(root),), "required", declared)
        assert problem.startswith(f"the configuration file {spelled} is reached through "), spelled
        assert f", in {claude}, {_DECLARED_WHAT} (sandbox.mode is required)" in problem
    # Spelled through a link, the declared directory is compared resolved.
    (base / "claude-alias").symlink_to(claude)
    alias = {str(base / "claude-alias"): _DECLARED_WHAT}
    assert "lies in" in config_file_problem(str(claude / "autoforge.yaml"), (), "required", alias)


def test_a_configuration_file_beside_or_above_a_providers_directory_passes(tmp_path):
    """A provider writes only inside its directory: neither a file beside it
    nor one whose own directory holds it is within reach."""
    base, root, outside = _tree(tmp_path)
    (outside / "a.yaml").write_text("version: 1\n")
    (base / "a.yaml").write_text("version: 1\n")
    declared = {str(outside / "claude"): _DECLARED_WHAT, str(base / "operator-x"): _DECLARED_WHAT}
    (outside / "claude").mkdir()
    (base / "operator-x").mkdir()
    assert config_file_problem(str(outside / "a.yaml"), (str(root),), "required", declared) == ""
    assert config_file_problem(str(base / "a.yaml"), (str(root),), "required", declared) == ""


def _placement_cfg(mode: str = "required"):
    cfg = default_config()
    cfg.sandbox.mode = mode
    return cfg


def test_the_placement_check_asks_every_configured_profiles_provider(tmp_path, monkeypatch):
    """#280 (R3-F1) where every command asks: the default profiles route
    ``analyze_execute`` and ``fix`` to Claude Code, whose dedicated
    ``CLAUDE_CONFIG_DIR`` every launch writes, so a file there is refused,
    outside any repository too; beside it the file passes, and under
    ``disabled`` nothing is asked."""
    base = Path(os.path.realpath(tmp_path))
    claude = base / "claude-autoforge"
    claude.mkdir()
    (base / "operator").mkdir()
    inside, beside = claude / "autoforge.yaml", base / "operator" / "autoforge.yaml"
    for path in (inside, beside):
        path.write_text("version: 1\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.chdir(base)  # no git repository here
    for mode in ("required", "preferred"):
        problem = config_file_placement_problem(_placement_cfg(mode), str(inside), base)
        assert problem.startswith(
            f"the configuration file {inside} lies in {claude}, the Claude Code configuration "
            "directory ($CLAUDE_CONFIG_DIR), which every sandboxed launch of claude (profile "
            f"analyze_execute, fix) writes (sandbox.mode is {mode})"
        )
        assert config_file_placement_problem(_placement_cfg(mode), str(beside), base) == ""
    assert config_file_placement_problem(_placement_cfg("disabled"), str(inside), base) == ""
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert config_file_placement_problem(_placement_cfg(), str(inside), base) == ""


@pytest.mark.parametrize("variable", ["PI_CODING_AGENT_DIR", "OPENCODE_DB"])
def test_the_placement_check_asks_a_provider_no_profile_names(tmp_path, monkeypatch, variable):
    """Which providers matter is not the file's to say: an agent that
    rewrote it could name another, and another configuration's launches
    write the directory too. No default profile uses Pi, yet a file in
    ``PI_CODING_AGENT_DIR`` is refused; nor, once no profile uses OpenCode,
    may a file lie in its database directory."""
    base = Path(os.path.realpath(tmp_path))
    directory = base / "provider-dir"
    directory.mkdir()
    cfg_file = directory / "autoforge.yaml"
    cfg_file.write_text("version: 1\n")
    value = str(directory) if variable == "PI_CODING_AGENT_DIR" else f"{directory}/af.db"
    monkeypatch.setenv(variable, value)
    cfg = _placement_cfg()
    provider = "pi" if variable == "PI_CODING_AGENT_DIR" else "opencode"
    for profile in cfg.profiles.values():
        profile.provider = "claude" if profile.provider == provider else profile.provider
    problem = config_file_placement_problem(cfg, str(cfg_file), base)
    assert problem.startswith(f"the configuration file {cfg_file} lies in {directory}, ")
    assert (
        f"which every sandboxed launch of {provider} (no profile of this file, but another "
        "configuration's) writes (sandbox.mode is required)"
    ) in problem


def test_one_directory_two_providers_write_is_named_for_both(tmp_path, monkeypatch):
    base = Path(os.path.realpath(tmp_path))
    shared = base / "shared"
    shared.mkdir()
    cfg_file = shared / "autoforge.yaml"
    cfg_file.write_text("version: 1\n")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(shared))
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(shared))
    problem = config_file_placement_problem(_placement_cfg(), str(cfg_file), base)
    assert problem.startswith(
        f"the configuration file {cfg_file} lies in {shared}, the Claude Code configuration "
        "directory ($CLAUDE_CONFIG_DIR), which every sandboxed launch of claude (profile "
        "analyze_execute, fix) writes; Pi's agent directory ($PI_CODING_AGENT_DIR), which every "
        "sandboxed launch of pi (no profile of this file, but another configuration's) writes "
        "(sandbox.mode is required)"
    )


def test_a_profile_whose_declaration_cannot_be_read_refuses_the_file(tmp_path):
    """Nothing then shows the file outside what that profile's launches write."""
    base = Path(os.path.realpath(tmp_path))
    cfg_file = base / "autoforge.yaml"
    cfg_file.write_text("version: 1\n")
    cfg = _placement_cfg()
    cfg.profiles["odd"] = ProfileConfig(name="odd", provider="no-such-provider")
    problem = config_file_placement_problem(cfg, str(cfg_file), base)
    assert problem.startswith(
        "the sandbox declaration of profile 'odd' (provider no-such-provider) cannot be read ("
    )
    assert problem.endswith(
        f"so the configuration file {cfg_file} is not shown to lie outside the directories its "
        "launches write"
    )
