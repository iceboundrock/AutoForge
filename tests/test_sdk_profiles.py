"""The opt-in SDK profiles (#150): the packaged helper manifest, the helper's
argv and a dry run's plan, switching a profile to an SDK provider and back,
and the commented example in every configuration backend.

The doctor rows are ``tests/test_doctor.py``'s, the policy
``tests/test_sdk_policy.py``'s (#149). Nothing here imports an SDK or needs
Node; the only processes are the Claude environment probe's, run on a
synthetic helper environment (:func:`helper_environment`), and its control
runs.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import subprocess
import sys
import venv
from pathlib import Path

import pytest

from autoforge import sdk_assets
from autoforge.config import (
    ProfileConfig,
    default_config,
    load_config_file,
    validate_profile,
    validate_required_profiles,
)
from autoforge.errors import ConfigurationError
from autoforge.profiles import REQUIRED_PROFILES
from autoforge.providers import ClaudeSdkProvider, OpenCodeSdkProvider, provider_for
from autoforge.sdk_assets import (
    CLAUDE_ENVIRONMENT_PROBE,
    CLAUDE_ENVIRONMENT_PROBE_FLAGS,
    STAGED_HELPER_DIR,
    HelperPin,
    checked_version,
    helper_problem,
    interpreter_pth_files,
    load_manifest,
    parse_claude_environment,
    parse_node_version,
    runtime_package,
)
from autoforge.sdk_policy import SDK_PROVIDERS
from autoforge.transitions import Phase
from tests.conftest import make_engine
from tests.test_config import pyyaml_hidden

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "autoforge.example.yaml"
PYTHON = "/opt/autoforge-claude-sdk/bin/python"
SERVER = "/opt/opencode-1.18.34/bin/opencode"
SECRET = "sk-planted-SECRET-150-0123456789abcdef"


def _claude(name: str = "analyze_execute", **options: str) -> ProfileConfig:
    return ProfileConfig(name, "claude-sdk", "fable", command=PYTHON, options=dict(options))


def _opencode(name: str = "review_round_1", **options: str) -> ProfileConfig:
    opts = {"server_command": SERVER, "credential_env": "OPENAI_API_KEY", **options}
    return ProfileConfig(name, "opencode-sdk", "openai/gpt-5.6-luna", command="node", options=opts)


# -- the manifest (ADR 0007 D2.2, D4.1, D4.2, D4.4) ------------------------------------
def test_the_manifest_pins_adr_0007s_pairs_and_ships_no_helper_yet():
    pins = load_manifest()
    assert sorted(pins) == sorted(SDK_PROVIDERS)
    claude, opencode = pins["claude-sdk"], pins["opencode-sdk"]
    assert (claude.envelope, claude.protocol, claude.file) == (
        "autoforge-claude-helper",
        1,
        "claude_helper.py",
    )
    assert claude.pair() == "claude-agent-sdk 0.2.163 with Claude Code 2.1.286"
    assert (claude.interpreter, claude.interpreter_minimum) == ("Python", (3, 10))
    assert (opencode.envelope, opencode.protocol, opencode.file) == (
        "autoforge-opencode-bridge",
        1,
        "opencode_bridge.mjs",
    )
    assert opencode.pair() == "@opencode-ai/sdk 1.18.34 with opencode-ai 1.18.34"
    assert (opencode.interpreter, opencode.interpreter_minimum) == ("Node", (24,))
    # #151 and #152 ship the programs; until then neither is in the package.
    for pin, issue in ((claude, "#151"), (opencode, "#152")):
        assert not pin.shipped and pin.implemented_by == issue
        assert "is not shipped with this AutoForge" in helper_problem(pin)


def test_the_manifest_is_read_from_the_package_never_the_source_tree():
    """The installed wheel reads its own copy (``tests/dist_check.py`` checks
    one installed outside any checkout); no ``helpers/`` directory is read."""
    assert sdk_assets.manifest_path() == (
        Path(sdk_assets.__file__).resolve().parent / "sdk_helpers" / "manifest.json"
    )
    assert sdk_assets.helpers_dir() == sdk_assets.manifest_path().parent


def _manifest(tmp_path: Path, mutate) -> Path:
    data = json.loads(sdk_assets.manifest_path().read_text(encoding="utf-8"))
    mutate(data)
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


@pytest.mark.parametrize(
    "mutate, problem",
    [
        (lambda d: d.update(schema=2), "schema is not 1"),
        (lambda d: d.update(extra=1), "exactly schema and helpers"),
        (lambda d: d["helpers"].pop("opencode-sdk"), "helpers must name exactly"),
        (lambda d: d["helpers"].update(pi={}), "helpers must name exactly"),
        (lambda d: d["helpers"]["claude-sdk"].pop("sha256"), "must hold exactly"),
        (lambda d: d["helpers"]["claude-sdk"].update(url="x"), "must hold exactly"),
        (lambda d: d["helpers"]["claude-sdk"].update(protocol=True), "protocol"),
        (lambda d: d["helpers"]["claude-sdk"].update(protocol=0), "protocol"),
        (lambda d: d["helpers"]["claude-sdk"].update(file="../x.py"), "file is not valid"),
        (lambda d: d["helpers"]["claude-sdk"].update(sha256="ABC"), "sha256 is not valid"),
        (lambda d: d["helpers"]["claude-sdk"]["sdk"].update(version=">=0.2"), "sdk.version"),
        (lambda d: d["helpers"]["opencode-sdk"]["runtime"].pop("name"), "runtime must hold"),
        (lambda d: d["helpers"]["claude-sdk"].update(implemented_by="151"), "implemented_by"),
    ],
)
def test_a_damaged_manifest_is_refused_whole_and_says_to_reinstall(tmp_path, mutate, problem):
    path = _manifest(tmp_path, mutate)
    with pytest.raises(ConfigurationError, match="reinstall AutoForge") as info:
        load_manifest(path)
    assert problem in str(info.value)


def test_an_unreadable_manifest_is_refused(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ConfigurationError, match="cannot be read: JSONDecodeError"):
        load_manifest(path)
    with pytest.raises(ConfigurationError, match="cannot be read: FileNotFoundError"):
        load_manifest(tmp_path / "absent.json")


def test_a_shipped_helper_is_usable_only_with_the_manifests_digest(tmp_path):
    body = b"# a helper\n"
    pin = dataclasses.replace(
        load_manifest()["claude-sdk"], sha256=hashlib.sha256(body).hexdigest()
    )
    assert "is missing from the installed package" in helper_problem(pin, tmp_path)
    (tmp_path / pin.file).write_bytes(body)
    assert helper_problem(pin, tmp_path) == ""
    (tmp_path / pin.file).write_bytes(body + b"import os\n")
    assert "does not match the manifest's sha256" in helper_problem(pin, tmp_path)


# -- the read-only probes' parsers -------------------------------------------------
@pytest.mark.parametrize(
    "text, found",
    [
        (
            '{"python": "3.12.4", "sdk": "0.2.163", "cli": "2.1.286", "bundled": true}',
            ((3, 12, 4), "0.2.163", "2.1.286", True),
        ),
        (
            '{"python": "3.11.0", "sdk": null, "cli": null, "bundled": false}\n',
            ((3, 11, 0), None, None, False),
        ),
        ('{"python": "3.12.4", "sdk": "0.2.163", "cli": "2.1.286"}', None),
        ('{"python": "3.12", "sdk": null, "cli": null, "bundled": false}', None),
        ('{"python": "3.12.4", "sdk": "a b", "cli": null, "bundled": false}', None),
        ('{"python": "3.12.4", "sdk": null, "cli": null, "bundled": 1}', None),
        ('{"python": "3.12.4", "sdk": null, "cli": null, "bundled": false}\nmore', None),
        (f"ANTHROPIC_API_KEY={SECRET}", None),
        ("", None),
    ],
)
def test_the_claude_environment_answer_is_read_strictly(text, found):
    env = parse_claude_environment(text)
    if found is None:
        assert env is None
    else:
        assert env is not None
        assert (env.python, env.sdk_version, env.cli_version, env.bundled) == found


@pytest.mark.parametrize(
    "text, found",
    [("v24.11.1\n", (24, 11, 1)), ("v22.0.0", (22, 0, 0)), ("24.1.0", None), ("v24", None)],
)
def test_node_versions_are_read_from_one_line(text, found):
    assert parse_node_version(text) == found


@pytest.mark.parametrize(
    "text, found",
    [
        ("1.18.34", "1.18.34"),
        ("1.18.34-beta.2", "1.18.34-beta.2"),
        ("2.0.27+build.5", "2.0.27+build.5"),
        ("v1.18.34", None),
        ("1.18.34 && echo", None),
        (SECRET, None),
        ("", None),
    ],
)
def test_only_text_that_reads_as_a_version_is_shown(text, found):
    assert checked_version(text) == found


# -- the OpenCode runtime's package (ADR 0007 D2.6's amendment) ---------------------
def _package(root: Path, name: str, document: object) -> Path:
    """An npm package directory under ``root/node_modules`` with its
    ``package.json`` and the binary npm links, ``bin/opencode.exe``."""
    package = root / "node_modules" / name
    (package / "bin").mkdir(parents=True)
    raw = document if isinstance(document, str) else json.dumps(document)
    (package / "package.json").write_text(raw, encoding="utf-8")
    binary = package / "bin" / "opencode.exe"
    binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    return binary


@pytest.mark.parametrize(
    "name, document, found",
    [
        ("opencode-ai", {"name": "opencode-ai", "version": "1.18.34"}, ("opencode-ai", "1.18.34")),
        (
            "@scope/opencode",
            {"name": "@scope/opencode", "version": "1.0.0"},
            ("@scope/opencode", "1.0.0"),
        ),
        ("opencode-ai", {"name": "opencode-ai"}, None),
        ("opencode-ai", {"name": "opencode-ai", "version": 1}, None),
        ("opencode-ai", ["opencode-ai", "1.18.34"], None),
        ("opencode-ai", "{not json", None),
    ],
    ids=["package", "scoped", "no-version", "number", "not-an-object", "not-json"],
)
def test_the_runtime_package_is_read_from_the_package_json_above_it(
    tmp_path, name, document, found
):
    assert runtime_package(str(_package(tmp_path, name, document))) == found


def test_a_runtime_in_no_npm_package_or_a_package_too_large_reads_as_none(tmp_path):
    loose = tmp_path / "opt" / "opencode"
    loose.parent.mkdir()
    loose.write_text("#!/bin/sh\n", encoding="utf-8")
    assert runtime_package(str(loose)) is None
    # The package directory itself is not a file inside it.
    assert runtime_package(str(tmp_path / "node_modules" / "opencode-ai")) is None
    binary = _package(tmp_path, "opencode-ai", {"name": "opencode-ai", "version": "1.18.34"})
    big = {"name": "opencode-ai", "version": "1.18.34", "pad": "x" * (1 << 20)}
    (binary.parent.parent / "package.json").write_text(json.dumps(big), encoding="utf-8")
    assert runtime_package(str(binary)) is None


def test_a_pth_file_beside_the_interpreter_as_found_or_as_resolved_is_seen(tmp_path):
    real = tmp_path / "base" / "python3.12"
    real.parent.mkdir()
    real.write_text("", encoding="utf-8")
    link = tmp_path / "env" / "bin" / "python"
    link.parent.mkdir(parents=True)
    link.symlink_to(real)
    assert interpreter_pth_files(str(link)) == []
    Path(f"{real}._pth").write_text("import site\n", encoding="utf-8")
    assert interpreter_pth_files(str(link)) == [f"{real}._pth"]
    Path(f"{link}._pth").write_text("import site\n", encoding="utf-8")
    assert interpreter_pth_files(str(link)) == [f"{link}._pth", f"{real}._pth"]


# -- the Claude environment probe, run for real (ADR 0007 D2.6's amendment) ---------
def helper_environment(root: Path, markers: Path, *, sdk: bool = True) -> Path:
    """A Claude helper environment as an operator makes one: a virtual
    environment of the running interpreter holding a synthetic
    ``claude-agent-sdk`` 0.2.163 (bundled Claude Code 2.1.286) whose package
    exits on import, and a live startup hook: an ``import`` line of a
    ``.pth`` file in its site-packages, which creates ``markers/pth`` when
    ``site`` runs it. Returns the interpreter."""
    venv.EnvBuilder(with_pip=False, symlinks=True).create(root)
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = root / "lib" / version / "site-packages"
    assert site.is_dir(), site
    (site / "zz_hook.pth").write_text(
        f"import os; open(os.path.join({str(markers)!r}, 'pth'), 'w').close()\n",
        encoding="utf-8",
    )
    if sdk:
        info = site / "claude_agent_sdk-0.2.163.dist-info"
        package = site / "claude_agent_sdk"
        (package / "_bundled").mkdir(parents=True)
        info.mkdir()
        (info / "METADATA").write_text(
            "Metadata-Version: 2.1\nName: claude-agent-sdk\nVersion: 0.2.163\n",
            encoding="utf-8",
        )
        (package / "__init__.py").write_text("raise SystemExit('imported')\n", encoding="utf-8")
        (package / "_cli_version.py").write_text('__cli_version__ = "2.1.286"\n', encoding="utf-8")
        binary = package / "_bundled" / "claude"
        binary.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        binary.chmod(0o755)
    return root / "bin" / "python"


def run_hooks(python: Path, cwd: Path) -> None:
    """The control run: the interpreter with ``-I`` alone, as the probe ran
    before #150's review, which runs the hook."""
    subprocess.run(
        [str(python), "-I", "-c", "pass"],
        env={"PATH": "/usr/bin:/bin"},
        cwd=cwd,
        timeout=60,
        check=True,
    )


def _probe(python: Path, cwd: Path):
    out = subprocess.run(
        [str(python), *CLAUDE_ENVIRONMENT_PROBE_FLAGS, "-c", CLAUDE_ENVIRONMENT_PROBE],
        capture_output=True,
        text=True,
        env={"PATH": "/usr/bin:/bin"},
        cwd=cwd,
        timeout=60,
        check=True,
    )
    return parse_claude_environment(out.stdout)


def test_the_claude_probe_reads_package_files_and_runs_no_startup_hook(tmp_path):
    """The probe as doctor runs it, on a helper environment whose ``.pth``
    import line is live (the control run shows it ran) and whose SDK exits
    on import: it reports the versions, imports nothing of the
    environment's and runs no hook (R1-F2 of #150's review)."""
    markers = tmp_path / "markers"
    markers.mkdir()
    python = helper_environment(tmp_path / "env", markers)
    run_hooks(python, tmp_path)
    assert [path.name for path in markers.iterdir()] == ["pth"]
    (markers / "pth").unlink()
    env = _probe(python, tmp_path)
    assert env is not None
    assert env.python == tuple(sys.version_info[:3])
    assert (env.sdk_version, env.cli_version, env.bundled) == ("0.2.163", "2.1.286", True)
    assert list(markers.iterdir()) == []
    version = f"python{sys.version_info.major}.{sys.version_info.minor}"
    site = python.parent.parent / "lib" / version / "site-packages"
    (site / "claude_agent_sdk" / "_bundled" / "claude").unlink()
    env = _probe(python, tmp_path)
    assert env is not None and env.bundled is False
    assert list(markers.iterdir()) == []


def test_the_claude_probe_finds_no_sdk_in_an_environment_without_one(tmp_path):
    """An environment without ``claude-agent-sdk`` reads as one, still
    without running a hook."""
    markers = tmp_path / "markers"
    markers.mkdir()
    env = _probe(helper_environment(tmp_path / "env", markers, sdk=False), tmp_path)
    assert env is not None
    assert (env.sdk_version, env.cli_version, env.bundled) == (None, None, False)
    assert list(markers.iterdir()) == []


# -- the helper argv and the plan ----------------------------------------------------
def test_each_name_selects_its_own_adapter_and_helper():
    claude, opencode = provider_for(_claude()), provider_for(_opencode())
    assert type(claude) is ClaudeSdkProvider and type(opencode) is OpenCodeSdkProvider
    assert claude.helper().envelope == "autoforge-claude-helper"
    assert opencode.helper().envelope == "autoforge-opencode-bridge"


def test_the_helper_argv_names_the_staged_helper_and_never_the_prompt():
    prompt = "IMPLEMENT ISSUE 150 " + "x" * 500
    assert provider_for(_claude()).build_command_for(_claude(), prompt) == [
        PYTHON,
        "-I",
        f"{STAGED_HELPER_DIR}/claude_helper.py",
    ]
    assert provider_for(_opencode()).build_command_for(_opencode(), prompt) == [
        "node",
        f"{STAGED_HELPER_DIR}/opencode_bridge.mjs",
    ]


@pytest.mark.parametrize("profile", [_claude(), _opencode()])
def test_the_plan_names_helper_runtime_policy_and_environment_by_name_only(profile, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", SECRET)
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)
    monkeypatch.setenv("GEMINI_API_KEY", SECRET)
    adapter = provider_for(profile)
    notes = adapter.plan_notes(profile, ("PATH", "HOME", "GEMINI_*"))
    text = "\n".join(notes)
    assert SECRET not in text
    pin = adapter.helper()
    assert pin.describe() in text and pin.pair() in text
    assert "the prompt travels in its start record on stdin, never in argv" in text
    assert "interaction_policy fail" in text
    assert "GEMINI_API_KEY" not in text  # another provider's credential is withheld
    credential = "ANTHROPIC_API_KEY" if profile.provider == "claude-sdk" else "OPENAI_API_KEY"
    assert f"PATH, HOME, {credential}," in text
    assert "every launch is refused in this release" in text
    assert f"<issue sandbox dir>/sdk/{profile.provider}/" in text
    if profile.provider == "opencode-sdk":
        assert f"{SERVER} serve --hostname=127.0.0.1 --port=0" in text
        assert "OPENCODE_SERVER_PASSWORD" not in text


def test_a_cli_provider_adds_no_plan_note():
    for profile in default_config().profiles.values():
        assert provider_for(profile).plan_notes(profile, ("PATH",)) == []


def test_a_dry_run_of_an_sdk_profile_renders_its_plan_and_launches_nothing(
    tmp_state_dir, fake_github
):
    """#150: the plan shows the helper's argv without the prompt, and notes
    what the argv does not; no SDK is imported, no process or state written."""
    cfg = default_config()
    cfg.profiles["analyze_execute"] = _claude()
    eng = make_engine(tmp_state_dir, "never", github=fake_github, cfg=cfg)
    eng.state.phase = Phase.ANALYZE_EXECUTE
    plan = eng.step(dry_run=True).plan
    assert plan.provider == "claude-sdk" and plan.profile_name == "analyze_execute"
    assert plan.command == [PYTHON, "-I", f"{STAGED_HELPER_DIR}/claude_helper.py"]
    assert plan.prompt_full and plan.prompt_full not in " ".join(plan.command)
    notes = "\n".join(plan.notes)
    assert "claude-sdk helper: autoforge-claude-helper (protocol 1" in notes
    assert "claude-sdk launch: every launch is refused in this release" in notes
    assert eng.provider.calls == [] and fake_github.effect_writes == []
    assert not eng.paths.state_file.exists()
    assert not [name for name in sys.modules if name.startswith("claude_agent_sdk")]


# -- switching a profile to an SDK provider and back (#129's rule) ------------------
def _toml(data: dict) -> str:
    """``{"version": 1, "profiles": {name: {key: str | {key: str}}}}`` as TOML."""
    lines = [f"version = {data['version']}"]
    for name, profile in data["profiles"].items():
        lines.append(f"[profiles.{name}]")
        nested = {k: v for k, v in profile.items() if isinstance(v, dict)}
        lines += [f"{k} = {json.dumps(v)}" for k, v in profile.items() if k not in nested]
        for key, table in nested.items():
            lines.append(f"[profiles.{name}.{key}]")
            lines += [f"{k} = {json.dumps(v)}" for k, v in table.items()]
    return "\n".join(lines) + "\n"


def _yaml(data: dict) -> str:
    lines = [f"version: {data['version']}", "profiles:"]
    for name, profile in data["profiles"].items():
        lines.append(f"  {name}:")
        for key, value in profile.items():
            if isinstance(value, dict):
                lines.append(f"    {key}:")
                lines += [f"      {k}: {v}" for k, v in value.items()]
            else:
                lines.append(f"    {key}: {value}")
    return "\n".join(lines) + "\n"


BACKENDS = ("json", "toml", "pyyaml", "subset")


def _load(tmp_path: Path, data: dict, backend: str, monkeypatch):
    suffix = {"json": "json", "toml": "toml"}.get(backend, "yaml")
    text = {"json": json.dumps(data), "toml": _toml(data)}.get(backend) or _yaml(data)
    path = tmp_path / f"{backend}-{len(list(tmp_path.iterdir()))}.{suffix}"
    path.write_text(text, encoding="utf-8")
    if backend == "subset":
        with pyyaml_hidden(monkeypatch):
            return load_config_file(path)
    if backend == "pyyaml":
        pytest.importorskip("yaml")
    return load_config_file(path)


SWITCH = {
    "version": 1,
    "profiles": {
        "analyze_execute": {"provider": "claude-sdk", "model": "fable", "command": PYTHON},
        "review_round_1": {
            "provider": "opencode-sdk",
            "model": "openai/gpt-5.6-luna",
            "command": "node",
            "options": {"server_command": SERVER, "credential_env": "OPENAI_API_KEY"},
        },
    },
}
SWITCH_BACK = {
    "version": 1,
    "profiles": {
        "analyze_execute": {"provider": "claude", "model": "fable"},
        "review_round_1": {"provider": "opencode"},
    },
}


@pytest.mark.parametrize("backend", BACKENDS)
def test_a_switched_profile_inherits_no_knob_and_switching_back_restores_the_argv(
    tmp_path, monkeypatch, backend
):
    defaults = default_config().profiles
    before = {name: defaults[name].build_command("PROMPT") for name in SWITCH["profiles"]}
    switched = _load(tmp_path, SWITCH, backend, monkeypatch)
    claude = switched.profile("analyze_execute")
    assert (claude.provider, claude.command, claude.extra_args, claude.options) == (
        "claude-sdk",
        PYTHON,
        [],
        {},
    )
    opencode = switched.profile("review_round_1")
    assert opencode.options == SWITCH["profiles"]["review_round_1"]["options"]
    assert "output_format" not in opencode.options and "auto_approve" not in opencode.options
    validate_required_profiles(switched, REQUIRED_PROFILES)
    # The profiles the override does not name are the defaults, untouched.
    assert switched.profile("fix") == defaults["fix"]
    restored = _load(tmp_path, SWITCH_BACK, backend, monkeypatch)
    for name, argv in before.items():
        assert restored.profile(name) == defaults[name]
        assert restored.profile(name).build_command("PROMPT") == argv


@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.parametrize(
    "provider, key, value",
    [
        ("claude-sdk", "command", ["python3"]),
        ("claude-sdk", "tools", ["Read"]),
        ("claude-sdk", "interaction_policy", ["fail"]),
        ("claude-sdk", "max_wait_seconds", [120]),
        ("opencode-sdk", "command", ["node"]),
        ("opencode-sdk", "server_command", ["opencode"]),
        ("opencode-sdk", "credential_env", ["OPENAI_API_KEY"]),
    ],
)
def test_a_list_for_an_sdk_executable_or_option_is_refused_never_passed_on(
    tmp_path, monkeypatch, backend, provider, key, value
):
    """#150: an option value is one scalar, and the loader turns a list into
    its Python text (``config._as_options``); for an SDK profile that text is
    refused wherever it lands, never handed to a helper as a command or a
    setting. The loader's reading of every other provider is unchanged."""
    profile = dict(
        SWITCH["profiles"]["analyze_execute" if provider == "claude-sdk" else "review_round_1"]
    )
    profile["options"] = dict(profile.get("options", {}))
    if key == "command":
        profile["command"] = value
    else:
        profile["options"][key] = value
    if not profile["options"]:
        del profile["options"]
    cfg = _load(tmp_path, {"version": 1, "profiles": {"fix": profile}}, backend, monkeypatch)
    with pytest.raises(ConfigurationError, match="profile 'fix'"):
        validate_profile(cfg.profile("fix"))


# -- the commented example ------------------------------------------------------------
def _example_with_the_sdk_block() -> tuple[str, str]:
    """The example with the SDK block uncommented in place of the active
    blocks it names, and the block alone as data."""
    lines = EXAMPLE.read_text(encoding="utf-8").splitlines()
    start = lines.index("  # sdk-example-start")
    end = lines.index("  # sdk-example-end")
    assert all(line.startswith("  # ") for line in lines[start + 1 : end])
    block = ["  " + line[len("  # ") :] for line in lines[start + 1 : end]]
    names = [line.strip()[:-1] for line in block if line.startswith("  ") and line[2] != " "]
    assert names == ["analyze_execute", "review_round_1"]
    edited = list(lines[:start]) + block + lines[end + 1 :]
    for name in names:
        first = edited.index(f"  {name}:")
        stop = next(i for i in range(first + 1, len(edited)) if edited[i] == "")
        del edited[first : stop + 1]
    return "\n".join(edited) + "\n", "\n".join(["profiles:", *block]) + "\n"


def test_the_example_yaml_with_the_sdk_block_enabled_loads_and_validates(tmp_path, monkeypatch):
    """The commented SDK example is a working pair of profiles in every
    backend, as the whole example with it enabled (PyYAML and the subset
    parser) and as an override of the built-in defaults (JSON, TOML and both
    YAML backends), and it leaves the merge gate closed."""
    text, block = _example_with_the_sdk_block()
    target = tmp_path / "autoforge.yaml"
    target.write_text(text, encoding="utf-8")
    loaded = []
    for hidden in (False, True):
        if hidden:
            with pyyaml_hidden(monkeypatch):
                loaded.append(load_config_file(target))
        else:
            pytest.importorskip("yaml")
            loaded.append(load_config_file(target))
    assert loaded[0] == loaded[1]
    override = tmp_path / "override.yaml"
    override.write_text("version: 1\n" + block, encoding="utf-8")
    with pyyaml_hidden(monkeypatch):
        data = {"version": 1, "profiles": load_config_file_data(override)}
    for backend in BACKENDS:
        loaded.append(_load(tmp_path, data, backend, monkeypatch))
    for cfg in loaded:
        claude, opencode = cfg.profile("analyze_execute"), cfg.profile("review_round_1")
        assert (claude.provider, claude.model, claude.effort, claude.command) == (
            "claude-sdk",
            "fable",
            "high",
            "/opt/autoforge-claude-sdk/bin/python",
        )
        assert claude.options == {
            "interaction_policy": "fail",
            "tools": "Read,Glob,Grep,Edit,Write,Bash",
        }
        assert (opencode.provider, opencode.command) == ("opencode-sdk", "node")
        assert opencode.options == {
            "server_command": "/opt/opencode-1.18.34/bin/opencode",
            "credential_env": "OPENAI_API_KEY",
        }
        validate_required_profiles(cfg, REQUIRED_PROFILES)
        for profile in (claude, opencode):
            validate_profile(profile)
        assert not cfg.merge_allowed_by_config and not cfg.safety.allow_merge
    assert all(cfg.profiles == loaded[0].profiles for cfg in loaded)


def load_config_file_data(path: Path) -> dict:
    """The ``profiles`` mapping of a YAML file, as the subset parser reads it."""
    from autoforge.config import _load_yaml

    data = _load_yaml(path)
    assert isinstance(data, dict)
    return data["profiles"]


def test_the_example_block_is_the_documented_one():
    """docs/configuration.md shows the same two profiles the example comments out."""
    text, _ = _example_with_the_sdk_block()
    guide = (REPO_ROOT / "docs" / "configuration.md").read_text(encoding="utf-8")
    assert "autoforge.example.yaml" in guide and "sdk-example-start" not in guide
    for needle in ("provider: claude-sdk", "provider: opencode-sdk", "server_command:"):
        assert needle in guide, needle


def test_helper_pin_is_frozen():
    pin = load_manifest()["claude-sdk"]
    assert isinstance(pin, HelperPin)
    with pytest.raises(dataclasses.FrozenInstanceError):
        pin.sha256 = "0" * 64  # type: ignore[misc]
