"""The container backend's image: its inputs, its content key and its build (ADR 0013 D7; #271).

**Inputs, from the operator's configuration only** (``sandbox.container.image``,
strict keys), never from the repository or an agent (:class:`ImageInputs`):

- ``base``: the base image, named by digest (``name@sha256:<64 hex>``); it
  holds every toolchain the repository needs, with ``python3`` and ``git``
  for AutoForge's helpers and ``npm`` when a CLI or integration is
  installed. A tag alone is refused: a mutable tag is not an input;
- ``claude_code``, ``opencode`` and ``pi``: each agent CLI's exact version
  (``@anthropic-ai/claude-code``, ``opencode-ai``,
  ``@earendil-works/pi-coding-agent``), or unset for a CLI the image does
  not carry;
- ``mcp_servers`` and ``skills``: npm packages, each ``name@version`` at
  an exact version. **Installing is not enabling** (D7): every launch keeps
  today's isolation flags, so an installed integration stays disabled
  until its own reviewed policy amendment.

AutoForge's own layer is added to them: the helpers (:data:`HELPERS`: the
watcher, the stager, the prover, the exporter, the measurement), by the
SHA-256 of their bytes, and this controller's version.

**The key** (:func:`image_key`) is the SHA-256 of every input, canonical
(lists sorted, so reordering a list is no change): it changes with each
input and not otherwise. An unchanged configuration reuses the image built
for it (:meth:`ImageBuilder.ensure`), found by the label
:data:`KEY_LABEL` and checked again on the image itself. Every launch then
names the image by its content-addressed id (:class:`ImageRef`), never by a
tag.

**The build** runs in the runtime's build container (``build``), never on
the host, from a context directory the controller writes fresh and fills
with exactly :func:`render_context`'s files: a ``Containerfile`` rendered
from the typed inputs alone and the helper files. No repository
``Dockerfile``, ``devcontainer.json`` or build script is read: nothing here
takes a repository path. The build gets no argument, secret, SSH agent,
mount or host network, and the ``Containerfile`` has no ``ARG`` and no
``ENV``, so no layer, build argument or label carries a credential. A base
that needs registry authentication is pulled by the operator first; the
client's configuration is empty (:mod:`autoforge.container_runtime`).

The image's ``/workspace`` and ``/cache`` are directories the agent's UID
owns, mode ``0700``: a new volume is filled from the image's directory at
its first mount, owner included, so the stager, running as the agent, can
write a fresh volume without any process running as root.

**No entrypoint.** A create gives PID 1's command (the watcher) after the
image's id, which a runtime appends to the image's entrypoint: a base
image with one would run its own program as PID 1, the watcher only as its
arguments, and the container's deadline and supervision would be gone. So
the ``Containerfile`` clears the base's (``ENTRYPOINT []``), and an image is
used only once its own configuration shows no entrypoint
(:func:`entrypoint_problem`): after its build, when the cache offers it,
and at the backend's probe.

Executor layer: the runtime CLI only, through the client; no workflow, no
state.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from . import (
    container_exporter,
    container_measure,
    container_prover,
    container_stager,
    container_watch,
    sandbox_container,
)
from .container_runtime import RuntimeClient
from .errors import SandboxCapabilityError
from .redaction import redact
from .sandbox_container import (
    AGENT_GID,
    AGENT_UID,
    CACHE_TARGET,
    HOME_MODE,
    HOME_PARENT,
    HOME_TARGET,
    WATCHER_GID,
    WATCHER_UID,
    WORKSPACE_TARGET,
    RuntimeProduct,
)

# 2: the image holds the agent's home, empty, under an otherwise empty
# HOME_PARENT, which a Podman launch's tmpfs copies up (PR #506).
# 3: the image names the agent's and the watcher's users and groups, so that
# Podman binds no /etc/passwd or /etc/group of its own (PR #506).
IMAGE_SCHEMA = 3
SETTING_IMAGE = "sandbox.container.image"
IMAGE_KEYS = ("base", "claude_code", "opencode", "pi", "mcp_servers", "skills")
# Where the helpers live in the image, and the file name each has there.
HELPER_DIR = "/opt/autoforge"
HELPERS: Mapping[str, str] = {
    "watch": "container_watch.py",
    "stager": "container_stager.py",
    "prover": "container_prover.py",
    "exporter": "container_exporter.py",
    "measure": "container_measure.py",
}
_HELPER_MODULES = {
    "container_watch.py": container_watch,
    "container_stager.py": container_stager,
    "container_prover.py": container_prover,
    "container_exporter.py": container_exporter,
    "container_measure.py": container_measure,
}
# Each agent CLI's npm package (D7).
CLI_PACKAGES: Mapping[str, str] = {
    "claude_code": "@anthropic-ai/claude-code",
    "opencode": "opencode-ai",
    "pi": "@earendil-works/pi-coding-agent",
}
# The command each package installs, whose ``--version`` the launch
# container reports (D7: versions are detected inside the container).
CLI_COMMANDS: Mapping[str, str] = {"claude_code": "claude", "opencode": "opencode", "pi": "pi"}
KEY_LABEL = "io.autoforge.image-key"
SCHEMA_LABEL = "io.autoforge.image-schema"
CONTAINERFILE = "Containerfile"
BUILD_TIMEOUT_SECONDS = 3600
BUILD_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

_BASE_RE = re.compile(r"[a-z0-9][a-z0-9._/:-]{0,254}@sha256:[0-9a-f]{64}")
_VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+(?:-[0-9A-Za-z.-]{1,64})?")
_PACKAGE_RE = re.compile(r"(?:@[a-z0-9][a-z0-9._-]{0,100}/)?[a-z0-9][a-z0-9._-]{0,100}")
_IMAGE_ID_RE = re.compile(r"(?:sha256:)?([0-9a-f]{64})")


def _refused(text: str) -> SandboxCapabilityError:
    return SandboxCapabilityError(text, capability="image")


def base_problem(value: object) -> str:
    """Why ``value`` is not a base image named by digest, or ""."""
    if not isinstance(value, str) or not _BASE_RE.fullmatch(value):
        return (
            "the base image is named by digest (name@sha256:<64 hex digits>); a tag alone is "
            "mutable and is never an input (ADR 0013 D7)"
        )
    return ""


def version_problem(value: object) -> str:
    """Why ``value`` is not an exact version, or ""."""
    if not isinstance(value, str) or not _VERSION_RE.fullmatch(value):
        return "an exact version (MAJOR.MINOR.PATCH, optionally -PRERELEASE); no range, tag or URL"
    return ""


def package_problem(value: object) -> str:
    """Why ``value`` is not an npm package pinned as ``name@version``, or ""."""
    if not isinstance(value, str) or "@" not in value[1:]:
        return "an npm package pinned as name@version"
    name, _, version = value.rpartition("@")
    if not _PACKAGE_RE.fullmatch(name):
        return "an npm package name (optionally @scope/name), no path, URL or git reference"
    problem = version_problem(version)
    return f"{value!r} is not pinned to {problem}" if problem else ""


@dataclass(frozen=True)
class ImageInputs:
    """The operator's image configuration, checked (D7). See the module docstring."""

    base: str
    claude_code: str = ""
    opencode: str = ""
    pi: str = ""
    mcp_servers: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        problem = base_problem(self.base)
        if problem:
            raise ValueError(f"{SETTING_IMAGE}.base: {problem}")
        for key in CLI_PACKAGES:
            value = getattr(self, key)
            if value and version_problem(value):
                raise ValueError(f"{SETTING_IMAGE}.{key}: {version_problem(value)}")
        for key in ("mcp_servers", "skills"):
            for item in getattr(self, key):
                problem = package_problem(item)
                if problem:
                    raise ValueError(f"{SETTING_IMAGE}.{key}: {problem}")

    def clis(self) -> tuple[tuple[str, str], ...]:
        """Each agent CLI the image carries, with its pinned version, in a stable order."""
        return tuple((key, getattr(self, key)) for key in CLI_PACKAGES if getattr(self, key))

    def packages(self) -> tuple[str, ...]:
        """Every npm package the image installs, pinned, in a stable order."""
        clis = [
            f"{CLI_PACKAGES[key]}@{getattr(self, key)}"
            for key in CLI_PACKAGES
            if getattr(self, key)
        ]
        return (*clis, *sorted(self.mcp_servers), *sorted(self.skills))


def helper_files() -> dict[str, bytes]:
    """Each helper's file name in the image and its bytes, at this controller's version."""
    files: dict[str, bytes] = {}
    for name, module in _HELPER_MODULES.items():
        path = module.__file__
        assert path is not None
        files[name] = Path(path).read_bytes()
    return files


def _controller_version() -> str:
    from . import __version__

    return __version__


@dataclass(frozen=True)
class KeyInputs:
    """Everything the image's key covers: the operator's inputs and AutoForge's layer."""

    inputs: ImageInputs
    helpers: Mapping[str, str] = field(default_factory=dict)
    controller: str = ""

    @classmethod
    def current(cls, inputs: ImageInputs) -> KeyInputs:
        helpers = {name: hashlib.sha256(data).hexdigest() for name, data in helper_files().items()}
        return cls(inputs, helpers, _controller_version())

    def canonical(self) -> bytes:
        data = {
            "schema": IMAGE_SCHEMA,
            "base": self.inputs.base,
            "cli": {key: getattr(self.inputs, key) for key in CLI_PACKAGES},
            "mcp_servers": sorted(self.inputs.mcp_servers),
            "skills": sorted(self.inputs.skills),
            "helpers": dict(sorted(self.helpers.items())),
            "controller": self.controller,
        }
        return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def image_key(key_inputs: KeyInputs) -> str:
    """``sha256:<hex>`` of every input: changes with each input and with nothing else."""
    return "sha256:" + hashlib.sha256(key_inputs.canonical()).hexdigest()


def home_steps() -> list[str]:
    """The build's steps for the agent's home: empty, the agent's, 0700, alone in its parent.

    A Podman launch mounts its tmpfs at :data:`HOME_PARENT` and copies this
    up, which is how the home comes to be the agent's there
    (:func:`autoforge.sandbox_container._home_tmpfs`); whatever the base keeps
    under :data:`HOME_PARENT` is removed, so that the tmpfs holds the home
    alone. A Docker launch mounts its tmpfs over the home itself.
    """
    return [
        f"mkdir -p {HOME_PARENT}",
        f"find {HOME_PARENT} -mindepth 1 -delete",
        f"mkdir {HOME_TARGET}",
        f"chown {AGENT_UID}:{AGENT_GID} {HOME_TARGET}",
        f"chmod {HOME_MODE:04o} {HOME_TARGET}",
    ]


def account_steps() -> list[str]:
    """The build's steps that name the agent's and the watcher's users and groups.

    Podman, given a ``--user`` its image's ``/etc/passwd`` or ``/etc/group``
    does not name, writes its own copy with that user or group added and
    binds it over the image's: a mount the policy does not make. So the
    image names them, unless its base already does under another name.
    """
    steps: list[str] = []
    for name, uid, gid, home in (
        ("autoforge-agent", AGENT_UID, AGENT_GID, HOME_TARGET),
        ("autoforge-watcher", WATCHER_UID, WATCHER_GID, "/"),
    ):
        steps.append(
            f"{{ grep -q '^[^:]*:[^:]*:{gid}:' /etc/group || "
            f"echo '{name}:x:{gid}:' >> /etc/group; }}"
        )
        steps.append(
            f"{{ grep -q '^[^:]*:[^:]*:{uid}:' /etc/passwd || "
            f"echo '{name}:x:{uid}:{gid}::{home}:/bin/sh' >> /etc/passwd; }}"
        )
    return steps


def layout_steps() -> list[str]:
    """The build's steps for the places a launch mounts over and the users it runs as.

    The workspace and cache are the agent's, so that a fresh volume mounted
    there is copied up from them with that owner; then the users and groups
    (:func:`account_steps`) and the home (:func:`home_steps`).
    """
    return [
        f"mkdir -p {WORKSPACE_TARGET} {CACHE_TARGET}",
        f"chown {AGENT_UID}:{AGENT_GID} {WORKSPACE_TARGET} {CACHE_TARGET}",
        f"chmod 0700 {WORKSPACE_TARGET} {CACHE_TARGET}",
        *account_steps(),
        *home_steps(),
    ]


def render_containerfile(inputs: ImageInputs, key: str) -> str:
    """The ``Containerfile`` from the typed inputs alone: no ``ARG``, ``ENV`` or repository file."""
    lines = [
        f"FROM {inputs.base}",
        "USER 0:0",
        f"COPY helpers/ {HELPER_DIR}/",
    ]
    checks = ["command -v python3 >/dev/null", "command -v git >/dev/null"]
    packages = inputs.packages()
    if packages:
        checks.append("command -v npm >/dev/null")
    steps = [
        "set -eu",
        *checks,
        f"chmod 0755 {HELPER_DIR}",
        f"chmod 0644 {HELPER_DIR}/*.py",
        *layout_steps(),
    ]
    if packages:
        steps.append(
            "npm install --global --no-fund --no-audit --no-update-notifier " + " ".join(packages)
        )
        steps.append("npm cache clean --force")
    lines.append("RUN " + " && \\\n    ".join(steps))
    # The base's entrypoint is cleared, so that PID 1 is the create's
    # command, the watcher; CMD too, so that nothing of the base's runs.
    lines.append("ENTRYPOINT []")
    lines.append("CMD []")
    lines.append(f'LABEL {KEY_LABEL}="{key}" {SCHEMA_LABEL}="{IMAGE_SCHEMA}"')
    return "\n".join(lines) + "\n"


def render_context(inputs: ImageInputs, key: str) -> dict[str, bytes]:
    """Every file of the build context, by its path in it: the ``Containerfile`` and the helpers."""
    files = {CONTAINERFILE: render_containerfile(inputs, key).encode("utf-8")}
    for name, data in helper_files().items():
        files[f"helpers/{name}"] = data
    return files


def build_argv(product: RuntimeProduct, context: str, iidfile: str, key: str) -> list[str]:
    """``build`` for the context the controller wrote, labelled with the image's key.

    Rendered by :func:`autoforge.sandbox_container.build_argv`, the only
    place a runtime flag of a create, an exec or a build is written.
    """
    labels = {KEY_LABEL: key, SCHEMA_LABEL: str(IMAGE_SCHEMA)}
    return sandbox_container.build_argv(
        product, os.path.join(context, CONTAINERFILE), context, iidfile, labels
    )


@dataclass(frozen=True)
class ImageRef:
    """The image a launch names: its content-addressed id (64 hex digits) and its key.

    ``clis`` are the agent CLIs its inputs pinned, ``(key, version)`` with
    :data:`CLI_PACKAGES`' keys: the backend reads each one's version inside
    a launch container and refuses an image that does not report it.
    """

    id: str
    key: str
    clis: tuple[tuple[str, str], ...] = ()

    @property
    def digest(self) -> str:
        """``sha256:<hex>``, as the launch's facts and validation's policy P record it."""
        return f"sha256:{self.id}"


def parse_image_id(text: str) -> str:
    """A runtime's image id, with or without ``sha256:``, as 64 hex digits; "" for none."""
    match = _IMAGE_ID_RE.fullmatch(text.strip())
    return match.group(1) if match else ""


def entrypoint_problem(client: RuntimeClient, image_id: str) -> str:
    """Why the image ``image_id`` could run something other than its create's command; "".

    Read from the image's own configuration: an entrypoint that is not
    empty (``null``, ``[]`` or ``""``) would be PID 1 in the watcher's
    place, with the watcher as its arguments. One the runtime cannot show
    is not proved empty either.
    """
    res = client.run(["image", "inspect", "--format", "{{json .Config.Entrypoint}}", image_id])
    if res.exit_code != 0:
        return f"the runtime did not show the image {image_id}'s entrypoint"
    try:
        entrypoint = json.loads(res.stdout or "")
    except ValueError:
        return f"the runtime showed no readable entrypoint for the image {image_id}"
    if entrypoint in (None, [], ""):
        return ""
    shown = redact(json.dumps(entrypoint))[:200]
    return (
        f"the image {image_id} has the entrypoint {shown}, which would run as PID 1 in the "
        "watcher's place; AutoForge's image clears it (ADR 0013 D7, D13)"
    )


class ImageBuilder:
    """The image for one configuration, reused when its key is unchanged, built otherwise."""

    def __init__(self, client: RuntimeClient, product: RuntimeProduct) -> None:
        self._client = client
        self._product = product

    def _labels(self, image_id: str) -> dict[str, str]:
        res = self._client.run(
            ["image", "inspect", "--format", "{{json .Config.Labels}}", image_id]
        )
        if res.exit_code != 0:
            return {}
        try:
            labels = json.loads(res.stdout)
        except ValueError:
            return {}
        return labels if isinstance(labels, dict) else {}

    def find(self, key: str) -> ImageRef | None:
        """The cached image built for ``key``, checked on the image itself; ``None`` if none."""
        args = ["images" if self._product is RuntimeProduct.PODMAN else "image"]
        if self._product is RuntimeProduct.DOCKER:
            args.append("ls")
        args += ["--no-trunc", "--filter", f"label={KEY_LABEL}={key}", "--format", "{{.ID}}"]
        res = self._client.run(args)
        if res.exit_code != 0:
            return None
        for line in res.stdout.splitlines():
            image_id = parse_image_id(line)
            if (
                image_id
                and self._labels(image_id).get(KEY_LABEL) == key
                and not entrypoint_problem(self._client, image_id)
            ):
                return ImageRef(image_id, key)
        return None

    def build(self, inputs: ImageInputs, key: str) -> ImageRef:
        """Build the image for ``inputs`` in the runtime's build container; its id."""
        context = tempfile.mkdtemp(prefix="autoforge-image-")
        try:
            for name, data in render_context(inputs, key).items():
                path = os.path.join(context, name)
                os.makedirs(os.path.dirname(path), mode=0o755, exist_ok=True)
                with open(path, "xb") as handle:
                    handle.write(data)
            iidfile = os.path.join(context, ".iid")
            res = self._client.run(
                build_argv(self._product, context, iidfile, key),
                timeout_seconds=BUILD_TIMEOUT_SECONDS,
                max_output_bytes=BUILD_MAX_OUTPUT_BYTES,
            )
            if res.exit_code != 0 or res.timed_out:
                tail = redact((res.stderr or res.stdout).strip()[-600:])
                ended = "timed out" if res.timed_out else f"exited {res.exit_code}"
                raise _refused(
                    f"the image build {ended}; no launch runs without its image"
                    + (f": {tail}" if tail else "")
                )
            try:
                image_id = parse_image_id(Path(iidfile).read_text())
            except OSError:
                image_id = ""
        finally:
            shutil.rmtree(context, ignore_errors=True)
        if not image_id or self._labels(image_id).get(KEY_LABEL) != key:
            raise _refused("the image build named no image that carries its key")
        problem = entrypoint_problem(self._client, image_id)
        if problem:
            raise _refused(f"the built image is not used: {problem}")
        return ImageRef(image_id, key)

    def ensure(self, inputs: ImageInputs) -> ImageRef:
        """The image for ``inputs``: the cached one for its key, or a new build."""
        key = image_key(KeyInputs.current(inputs))
        image = self.find(key) or self.build(inputs, key)
        return dataclasses.replace(image, clis=inputs.clis())
