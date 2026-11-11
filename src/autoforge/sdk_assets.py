"""The SDK helpers' static manifest and the read-only facts about their installs (#150).

``src/autoforge/sdk_helpers/manifest.json`` is package data (ADR 0007
D2.2): for each SDK provider it records the helper program the adapter
stages per launch, its envelope and protocol (D4.5), whether this release
ships it (its sha256, or ``null``), the issue that ships it, and the
compatibility pair it is built and tested against (D4.1, D4.2), with the
interpreter floor (D2.3: Python 3.10; D4.4: Node 24). A pair moves only by
an amendment of ADR 0007 that updates this file (D4.6).

The manifest is read relative to this package (``__file__``), like the
prompt templates, so an installed wheel reads its own copy and never the
source checkout's ``helpers/`` directory. Nothing imports
``autoforge.sdk_helpers``: it is a data directory, and the helper programs
in it are separate executables (D2.1).

Provider layer. It reads the manifest and, for a shipped helper, the
helper's bytes for their digest; it starts no process, imports no SDK and
reaches no network. It also holds what ``doctor`` asks a Claude helper
environment (:data:`CLAUDE_ENVIRONMENT_PROBE`), how the answers of the
read-only probes are parsed, and the reads ``doctor`` makes instead of
running something that would write: an interpreter's ``._pth`` file
(:func:`interpreter_pth_files`) and an OpenCode runtime's ``package.json``
(:func:`runtime_package`). The controller never imports an SDK to learn
its version, and never runs the OpenCode runtime (D2.6 and its amendment).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from pathlib import Path

from .errors import ConfigurationError
from .sdk_policy import SDK_PROVIDERS

HELPERS_DIRNAME = "sdk_helpers"
MANIFEST_NAME = "manifest.json"
# The version of the manifest's own layout; it moves when a field is added
# or changes meaning.
MANIFEST_SCHEMA = 1
# What a plan and the run log name the per-launch directory a helper is
# staged in (D2.2): a role, since the directory is made for the launch.
STAGED_HELPER_DIR = "<per-launch helper directory>"

_HELPER_KEYS = (
    "envelope",
    "protocol",
    "file",
    "sha256",
    "implemented_by",
    "interpreter",
    "sdk",
    "runtime",
)
_ENVELOPE_RE = re.compile(r"autoforge-[a-z]+(?:-[a-z]+)*")
_FILE_RE = re.compile(r"[a-z][a-z0-9_]*\.(?:py|mjs)")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_ISSUE_RE = re.compile(r"#[1-9][0-9]{0,5}")
_NAME_RE = re.compile(r"[@A-Za-z][@/A-Za-z0-9 ._-]{0,63}")
_PIN_RE = re.compile(r"[0-9]+(?:\.[0-9]+){2}")
_MINIMUM_RE = re.compile(r"[0-9]+(?:\.[0-9]+){0,2}")


def helpers_dir() -> Path:
    """The installed package's ``sdk_helpers`` directory."""
    return Path(__file__).resolve().parent / HELPERS_DIRNAME


def manifest_path() -> Path:
    return helpers_dir() / MANIFEST_NAME


def dotted(text: str) -> tuple[int, ...]:
    """``"3.10"`` as ``(3, 10)``; the manifest's own versions, already checked."""
    return tuple(int(part) for part in text.split("."))


def shown_version(version: tuple[int, ...]) -> str:
    return ".".join(str(part) for part in version)


@dataclass(frozen=True)
class HelperPin:
    """One SDK provider's helper and compatibility pair, as the manifest records it."""

    provider: str
    envelope: str
    protocol: int
    file: str
    # ``None``: this release does not ship the helper program.
    sha256: str | None
    implemented_by: str
    interpreter: str
    interpreter_minimum: tuple[int, ...]
    sdk_package: str
    sdk_version: str
    runtime: str
    runtime_version: str

    @property
    def shipped(self) -> bool:
        return self.sha256 is not None

    def pair(self) -> str:
        """The supported pair, as an operator reads it."""
        return f"{self.sdk_package} {self.sdk_version} with {self.runtime} {self.runtime_version}"

    def describe(self) -> str:
        """The helper program by envelope, protocol and packaged file."""
        return f"{self.envelope} (protocol {self.protocol}, sdk_helpers/{self.file})"

    def staged_path(self) -> str:
        """Where a launch runs the helper from, by role (D2.2)."""
        return f"{STAGED_HELPER_DIR}/{self.file}"


def _damaged(path: Path, problem: str) -> ConfigurationError:
    return ConfigurationError(
        f"the SDK helper manifest {path} is damaged ({problem}): reinstall AutoForge"
    )


def _text(raw: object, pattern: re.Pattern[str], path: Path, what: str) -> str:
    if not isinstance(raw, str) or not pattern.fullmatch(raw):
        raise _damaged(path, f"{what} is not valid")
    return raw


def _pin(provider: str, raw: object, path: Path) -> HelperPin:
    where = f"helpers.{provider}"
    if not isinstance(raw, dict) or set(raw) != set(_HELPER_KEYS):
        raise _damaged(path, f"{where} must hold exactly {', '.join(_HELPER_KEYS)}")
    protocol = raw["protocol"]
    if type(protocol) is not int or protocol < 1:
        raise _damaged(path, f"{where}.protocol is not a positive whole number")
    sha256 = raw["sha256"]
    if sha256 is not None:
        sha256 = _text(sha256, _SHA256_RE, path, f"{where}.sha256")
    parts: dict[str, tuple[str, str]] = {}
    for key, second, pattern in (
        ("interpreter", "minimum", _MINIMUM_RE),
        ("sdk", "version", _PIN_RE),
        ("runtime", "version", _PIN_RE),
    ):
        part = raw[key]
        first = "package" if key == "sdk" else "name"
        if not isinstance(part, dict) or set(part) != {first, second}:
            raise _damaged(path, f"{where}.{key} must hold exactly {first} and {second}")
        parts[key] = (
            _text(part[first], _NAME_RE, path, f"{where}.{key}.{first}"),
            _text(part[second], pattern, path, f"{where}.{key}.{second}"),
        )
    return HelperPin(
        provider=provider,
        envelope=_text(raw["envelope"], _ENVELOPE_RE, path, f"{where}.envelope"),
        protocol=protocol,
        file=_text(raw["file"], _FILE_RE, path, f"{where}.file"),
        sha256=sha256,
        implemented_by=_text(raw["implemented_by"], _ISSUE_RE, path, f"{where}.implemented_by"),
        interpreter=parts["interpreter"][0],
        interpreter_minimum=dotted(parts["interpreter"][1]),
        sdk_package=parts["sdk"][0],
        sdk_version=parts["sdk"][1],
        runtime=parts["runtime"][0],
        runtime_version=parts["runtime"][1],
    )


def load_manifest(path: Path | None = None) -> dict[str, HelperPin]:
    """Every SDK provider's :class:`HelperPin`, read strictly from the manifest.

    A missing or malformed manifest, an unknown or missing key, or a
    provider other than the SDK providers is a :class:`ConfigurationError`
    that says to reinstall: the file ships with the controller, so a
    difference is a damaged install, never something to adapt to.
    """
    path = manifest_path() if path is None else path
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise _damaged(path, f"it cannot be read: {type(exc).__name__}") from None
    if not isinstance(data, dict) or set(data) != {"schema", "helpers"}:
        raise _damaged(path, "it must hold exactly schema and helpers")
    if data["schema"] != MANIFEST_SCHEMA or type(data["schema"]) is not int:
        raise _damaged(path, f"schema is not {MANIFEST_SCHEMA}")
    helpers = data["helpers"]
    if not isinstance(helpers, dict) or sorted(helpers) != sorted(SDK_PROVIDERS):
        raise _damaged(path, f"helpers must name exactly {', '.join(SDK_PROVIDERS)}")
    return {provider: _pin(provider, helpers[provider], path) for provider in SDK_PROVIDERS}


def helper_pin(provider: str) -> HelperPin:
    """``provider``'s pin from the installed manifest."""
    return load_manifest()[provider]


def helper_problem(pin: HelperPin, directory: Path | None = None) -> str:
    """Why ``pin``'s helper program cannot be staged, or '' when it can (D2.2).

    Not shipped in this release, missing from the installed package, or
    bytes whose sha256 is not the manifest's (a damaged or foreign file).
    """
    if not pin.shipped:
        return (
            f"its helper program {pin.describe()} is not shipped with this AutoForge: "
            f"it is {pin.implemented_by}'s"
        )
    path = (helpers_dir() if directory is None else directory) / pin.file
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return (
            f"its helper program {pin.describe()} is missing from the installed package: "
            "reinstall AutoForge"
        )
    if digest != pin.sha256:
        return (
            f"its helper program {pin.describe()} does not match the manifest's sha256 (a "
            "damaged or foreign file): reinstall AutoForge"
        )
    return ""


# -- read-only version probes (ADR 0007 D2.6 and its amendment) --------------
# What ``doctor`` runs with a ``claude-sdk`` profile's interpreter, as
# ``[interpreter, *CLAUDE_ENVIRONMENT_PROBE_FLAGS, "-c",
# CLAUDE_ENVIRONMENT_PROBE]``. ``-I`` alone still imports ``site``, which
# runs the environment's startup hooks (an ``import`` line of any ``.pth``
# file in its site-packages, ``sitecustomize``) with the operator's access
# before the probe's first line. ``-S`` keeps ``site`` out, so the probe
# finds the site-packages directories itself, as ``site`` would without
# running a hook: the virtual environment of a ``pyvenv.cfg`` beside the
# interpreter or one directory up (with the base installation's when it
# includes system site-packages), else the installation's own prefix.
# ``-B`` writes no bytecode for the standard-library modules it imports.
CLAUDE_ENVIRONMENT_PROBE_FLAGS = ("-I", "-S", "-B")
# The interpreter's version, the installed ``claude-agent-sdk``'s version
# from its package metadata, and the bundled Claude Code's version read as
# text from ``_cli_version.py``, with whether the bundled executable exists
# (D2.4). It imports no SDK module and nothing outside the standard library,
# and starts nothing: ``importlib.metadata`` reads the distribution's files,
# and the two package files are read, never run. It prints one JSON line.
CLAUDE_ENVIRONMENT_PROBE = """\
import json, os, re, site, sys
from importlib import metadata
out = {"python": "%d.%d.%d" % sys.version_info[:3], "sdk": None, "cli": None, "bundled": False}
bindir = os.path.dirname(os.path.abspath(sys.executable))
root = os.path.dirname(bindir)
prefixes = [sys.prefix, sys.exec_prefix]
for conf in (os.path.join(bindir, "pyvenv.cfg"), os.path.join(root, "pyvenv.cfg")):
    if os.path.isfile(conf):
        system = "true"
        with open(conf, encoding="utf-8") as f:
            for line in f:
                key, eq, value = line.partition("=")
                if eq and key.strip().lower() == "include-system-site-packages":
                    system = value.strip().lower()
        prefixes = [root]
        if system == "true":
            prefixes += [sys.base_prefix, sys.base_exec_prefix]
        break
lib = "python%d.%d" % sys.version_info[:2]
libdirs = (getattr(sys, "platlibdir", "lib"), "lib")
dirs = [os.path.join(p, d, lib, "site-packages") for p in prefixes for d in libdirs]
dirs = [d for d in dict.fromkeys(dirs + site.getsitepackages(prefixes)) if os.path.isdir(d)]
dist = next(iter(metadata.distributions(name="claude-agent-sdk", path=dirs)), None)
if dist is not None:
    out["sdk"] = dist.version
    try:
        with open(dist.locate_file("claude_agent_sdk/_cli_version.py"), encoding="utf-8") as f:
            found = re.search(r"__cli_version__\\s*=\\s*['\\"]([^'\\"]{1,64})['\\"]", f.read(4096))
        out["cli"] = found.group(1) if found else None
    except OSError:
        pass
    binary = str(dist.locate_file("claude_agent_sdk/_bundled/claude"))
    out["bundled"] = os.path.isfile(binary) and os.access(binary, os.X_OK)
print(json.dumps(out))
"""


def interpreter_pth_files(executable: str) -> list[str]:
    """The ``._pth`` files beside ``executable``, as found and as resolved.

    Such a file overrides the interpreter's path and, with an ``import
    site`` line, imports ``site`` even under ``-S``, and with it the
    environment's startup hooks. ``doctor`` therefore runs no probe with an
    interpreter that has one (ADR 0007 D2.6's amendment). It only looks:
    the file is not opened.
    """
    found = []
    for path in dict.fromkeys((executable, os.path.realpath(executable))):
        if os.path.lexists(path + "._pth"):
            found.append(path + "._pth")
    return found


# The most of a runtime's package.json that is read.
_PACKAGE_JSON_LIMIT = 1 << 20


def runtime_package(executable: str) -> tuple[str, str] | None:
    """``(name, version)`` of the npm package the executable file at
    ``executable`` (an absolute, resolved path) lies in, or ``None``.

    The package is the innermost ``node_modules/<name>`` (or
    ``node_modules/@scope/<name>``) directory above the file, and its
    ``package.json`` is read as data, at most :data:`_PACKAGE_JSON_LIMIT`
    bytes; ``None`` when there is no such directory or that file is not a
    JSON object naming both as text. ``doctor`` reads the OpenCode
    runtime's version this way because running the runtime, even for
    ``--version``, creates directories in the operator's home before it
    reads its arguments (ADR 0007 D2.6's amendment). ``opencode-ai``'s
    install puts its platform binary at ``bin/opencode.exe`` inside the
    package (D2.5), where npm's ``bin`` link points. The values are
    untrusted text: a caller shows only what it has checked.
    """
    parts = Path(executable).parts
    marks = [index for index, part in enumerate(parts) if part == "node_modules"]
    if not marks:
        return None
    index = marks[-1]
    end = index + (3 if index + 1 < len(parts) and parts[index + 1].startswith("@") else 2)
    if end >= len(parts):
        return None
    package = Path(*parts[:end]) / "package.json"
    try:
        if not package.is_file():
            return None
        with package.open("rb") as f:
            raw = f.read(_PACKAGE_JSON_LIMIT + 1)
        if len(raw) > _PACKAGE_JSON_LIMIT:
            return None
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    name, version = data.get("name"), data.get("version")
    if not isinstance(name, str) or not isinstance(version, str):
        return None
    return name, version


# A version this module shows: a release with an optional suffix, never
# free text (a probe's answer is untrusted output).
_SHOWN_VERSION_RE = re.compile(r"[0-9]+(?:\.[0-9]+){1,3}(?:[.+-]?[0-9A-Za-z]+){0,3}")
_PYTHON_RE = re.compile(r"([0-9]+)\.([0-9]+)\.([0-9]+)")
_NODE_RE = re.compile(r"v([0-9]+)\.([0-9]+)\.([0-9]+)")


@dataclass(frozen=True)
class ClaudeSdkEnvironment:
    """What :data:`CLAUDE_ENVIRONMENT_PROBE` found in a helper environment."""

    python: tuple[int, int, int]
    # ``None``: ``claude-agent-sdk`` is not installed there.
    sdk_version: str | None
    # ``None``: no ``_cli_version.py`` could be read.
    cli_version: str | None
    bundled: bool


def checked_version(value: str) -> str | None:
    """``value`` when it reads as a version (a release with an optional
    suffix), else ``None``: untrusted text is shown only once it is that."""
    return value if _SHOWN_VERSION_RE.fullmatch(value) else None


def _version_field(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _SHOWN_VERSION_RE.fullmatch(value):
        raise ValueError("not a version")
    return value


def parse_claude_environment(text: str) -> ClaudeSdkEnvironment | None:
    """The probe's one JSON line, or ``None`` when it is not exactly that
    (the output is then never quoted)."""
    lines = text.strip().splitlines()
    if len(lines) != 1:
        return None
    try:
        data = json.loads(lines[0])
        if not isinstance(data, dict) or set(data) != {"python", "sdk", "cli", "bundled"}:
            return None
        python = _PYTHON_RE.fullmatch(data["python"]) if isinstance(data["python"], str) else None
        if python is None or type(data["bundled"]) is not bool:
            return None
        major, minor, micro = (int(part) for part in python.groups())
        return ClaudeSdkEnvironment(
            python=(major, minor, micro),
            sdk_version=_version_field(data["sdk"]),
            cli_version=_version_field(data["cli"]),
            bundled=data["bundled"],
        )
    except ValueError:
        return None


def parse_node_version(text: str) -> tuple[int, int, int] | None:
    """``(major, minor, patch)`` from ``node --version`` (``v24.11.1``), or
    ``None`` when the output is not exactly one such line."""
    lines = text.strip().splitlines()
    match = _NODE_RE.fullmatch(lines[0].strip()) if len(lines) == 1 else None
    if match is None:
        return None
    major, minor, patch = (int(part) for part in match.groups())
    return major, minor, patch
