"""The container backend's runtime: which one, what it says it is, and how it is called (#271).

ADR 0013 D8 (detection), D13 (the controller's runtime CLI calls), D16 (the
runtime check) and D6 item 10 (read fresh at every call). Three things:

**Detection** (:func:`detect`, D8). ``sandbox.container.runtime`` names the
runtime, ``podman`` or ``docker``. Unset, the backend uses Podman when
``podman`` is on the controller's ``PATH``, and Docker otherwise. It runs
once and never falls through: a runtime that is found and then fails its
check refuses the step naming the runtime and the setting, and the other
runtime is never tried, so which runtime a run uses never depends on a
transient failure.

**The client** (:class:`RuntimeClient`, D13). Every runtime CLI call is
controller plumbing, never sandboxed, through the executor as an argv list
with an allow-listed environment (:data:`CLIENT_ENV_ALLOWLIST`, nothing
else of the operator's) and a client configuration directory that is
created empty for that one call and removed after it (``DOCKER_CONFIG``,
and Podman's ``REGISTRY_AUTH_FILE`` inside it, which does not exist): no
credential store, credential helper or CLI plugin of the operator's runs,
and no call leaves anything there for the next. The endpoint is the
runtime's local one (:func:`endpoint_refusal`): a ``DOCKER_HOST`` or
``CONTAINER_HOST`` naming anything but a local Unix socket, a
``DOCKER_CONTEXT`` other than ``default`` and a ``CONTAINER_CONNECTION``
are refused before any call, and an operator's Docker context is never
read (the configuration is empty).

**Identity** (:meth:`RuntimeClient.identity`, D8, D16). What the runtime
is comes from its own structured report, ``version`` and ``info`` as JSON,
never from the executable's name: Fedora's ``podman-docker`` wrapper is a
``docker`` that ``exec``\\ s Podman, and it is identified as Podman. The
report gives the product (Docker Engine or Podman), its version and API
version, whether it is rootless (Docker's ``name=rootless`` security
option, Podman's ``host.security.rootless``), its endpoint and its store:

- Docker Engine: ``ID`` (the engine's id) and ``DockerRootDir``;
- Podman: ``store.graphRoot``, ``store.runRoot`` and
  ``store.graphDriverName``.

Those fields (:data:`STORE_FIELDS`) are what #272 binds a journal to: a
store whose fields changed is another store. The report is read from the
runtime at every call and never kept here (D6 item 10): a store that moves
between two calls in one process is reported moved by the second. A report
that is neither runtime's shape, holds both, or names Docker Desktop or a
remote Podman service is refused as unverified (D16's table).

Executor layer: no workflow, no journal, no state. A refusal is
:class:`~autoforge.errors.SandboxCapabilityError` naming the capability,
before anything is created; nothing here prints an environment value.
"""

from __future__ import annotations

import json
import os
import posixpath
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from .errors import SandboxCapabilityError
from .executor import ExecutionRequest, ExecutionResult, execute
from .redaction import redact
from .sandbox import BackendKind
from .sandbox_backend import RuntimeReport
from .sandbox_container import RuntimeProduct

Runner = Callable[[ExecutionRequest], ExecutionResult]

# The setting that names the runtime (ADR 0013 D19), and its values.
SETTING_RUNTIME = "sandbox.container.runtime"
RUNTIME_SETTINGS = ("podman", "docker")

# The operator's variables a runtime CLI call keeps: where it finds its
# programs, its own storage and runtime directories, and the engine's own
# configuration files (which decide the store; dropping one would move
# it). Nothing that holds a credential, and no proxy: an image is never
# pulled: a create never pulls (sandbox_container's create options).
CLIENT_ENV_ALLOWLIST = (
    "PATH",
    "HOME",
    "LANG",
    "LC_ALL",
    "TMPDIR",
    "XDG_RUNTIME_DIR",
    "XDG_DATA_HOME",
    "XDG_CONFIG_HOME",
    "CONTAINERS_CONF",
    "CONTAINERS_CONF_OVERRIDE",
    "CONTAINERS_STORAGE_CONF",
    "CONTAINERS_REGISTRIES_CONF",
)
# The endpoint variables, kept only once :func:`endpoint_refusal` admits them.
ENDPOINT_VARIABLES = ("DOCKER_HOST", "CONTAINER_HOST")
# Variables that select an endpoint by a name defined elsewhere: refused,
# since the configuration they would be read from is never read.
NAMED_ENDPOINT_VARIABLES = ("DOCKER_CONTEXT", "CONTAINER_CONNECTION")

# Docker's endpoint when no DOCKER_HOST is given and the configuration is
# empty, so no context is read.
DOCKER_DEFAULT_ENDPOINT = "unix:///var/run/docker.sock"
PODMAN_LOCAL_ENDPOINT = "local"

# The fields of each runtime's own ``info`` that identify its store (D16, D6
# item 9), as dotted paths into the report.
STORE_FIELDS: Mapping[RuntimeProduct, tuple[str, ...]] = {
    RuntimeProduct.DOCKER: ("ID", "DockerRootDir"),
    RuntimeProduct.PODMAN: ("store.graphRoot", "store.runRoot", "store.graphDriverName"),
}

PRODUCT_NAMES: Mapping[RuntimeProduct, str] = {
    RuntimeProduct.DOCKER: "Docker Engine",
    RuntimeProduct.PODMAN: "Podman",
}

# A report call's bounds: ``version`` and ``info`` are read-only and quick.
REPORT_TIMEOUT_SECONDS = 60
REPORT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024
_JSON_FORMAT = "{{json .}}"
_MAX_FIELD = 4096

# The capabilities a refusal names (ADR 0013 D16 items 1 and 2).
CAPABILITY_RUNTIME = "container runtime"
CAPABILITY_ENDPOINT = "local endpoint"
CAPABILITY_IDENTITY = "runtime identity"


def _refused(text: str, capability: str = CAPABILITY_RUNTIME) -> SandboxCapabilityError:
    return SandboxCapabilityError(text, capability=capability)


# -- detection (D8) --------------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeChoice:
    """The runtime detection chose: the name looked up and the executable found.

    ``named`` is the CLI's name, never proof of what it is
    (:meth:`RuntimeClient.identity` says that); ``how`` says why it was
    chosen, for a refusal to name: the setting, or the detection order.
    """

    named: RuntimeProduct
    executable: str
    how: str


def detect(
    setting: str | None,
    environ: Mapping[str, str],
    which: Callable[[str, str | None], str | None] | None = None,
) -> RuntimeChoice:
    """The runtime, once: the setting's, else Podman when on ``PATH``, else Docker (D8).

    ``which(name, path)`` finds an executable on ``path`` (default
    :func:`shutil.which`); ``environ`` gives the controller's ``PATH``.
    Never falls through: a set runtime that is missing is refused, and so
    is an unset one when neither is found. The choice is final for the
    step; a failure afterwards refuses it, it never tries the other.
    """
    find = which or (lambda name, path: shutil.which(name, path=path))
    path = environ.get("PATH", "")
    if setting is not None:
        if setting not in RUNTIME_SETTINGS:
            raise _refused(
                f"{SETTING_RUNTIME} is {setting!r}; it names podman or docker, nothing else"
            )
        product = RuntimeProduct(setting)
        found = find(setting, path)
        if not found:
            raise _refused(
                f"{SETTING_RUNTIME} names {setting}, and no {setting} is on the controller's "
                "PATH; the other runtime is never tried instead"
            )
        return RuntimeChoice(product, os.path.abspath(found), f"{SETTING_RUNTIME}: {setting}")
    podman = find("podman", path)
    if podman:
        return RuntimeChoice(
            RuntimeProduct.PODMAN,
            os.path.abspath(podman),
            f"{SETTING_RUNTIME} unset, and podman is on the controller's PATH",
        )
    docker = find("docker", path)
    if docker:
        return RuntimeChoice(
            RuntimeProduct.DOCKER,
            os.path.abspath(docker),
            f"{SETTING_RUNTIME} unset, and docker (not podman) is on the controller's PATH",
        )
    raise _refused(
        f"{SETTING_RUNTIME} is unset, and neither podman nor docker is on the controller's PATH"
    )


# -- the endpoint (D13) ----------------------------------------------------------------


def _local_socket(value: str) -> bool:
    if not value.startswith("unix://"):
        return False
    path = value[len("unix://") :]
    return path.startswith("/") and posixpath.normpath(path) == path and "\0" not in path


def endpoint_refusal(environ: Mapping[str, str]) -> SandboxCapabilityError | None:
    """Why the operator's environment names an endpoint the MVP refuses, or ``None`` (D13, D16).

    A ``DOCKER_HOST`` or ``CONTAINER_HOST`` must name a local Unix socket
    by an absolute path; a TCP, SSH or any other endpoint is another
    machine's (#276, #277). ``DOCKER_CONTEXT`` (other than ``default``) and
    ``CONTAINER_CONNECTION`` name an endpoint defined in a configuration
    the controller never reads, so they are refused too. The refusal names
    the variable, never its value.
    """
    for name in ENDPOINT_VARIABLES:
        value = environ.get(name)
        if value is not None and not _local_socket(value):
            return _refused(
                f"{name} names an endpoint that is not a local Unix socket; the container "
                "backend drives only the operator's local rootless runtime (ADR 0013 D13, D16)",
                CAPABILITY_ENDPOINT,
            )
    context = environ.get("DOCKER_CONTEXT")
    if context is not None and context != "default":
        return _refused(
            "DOCKER_CONTEXT names a Docker context, and the runtime client runs with an empty "
            "configuration, where no context is defined; name the local socket with "
            "DOCKER_HOST=unix:///... instead (ADR 0013 D13)",
            CAPABILITY_ENDPOINT,
        )
    if environ.get("CONTAINER_CONNECTION") is not None:
        return _refused(
            "CONTAINER_CONNECTION names a Podman connection, which may be another machine's; "
            "the container backend drives only the local runtime (ADR 0013 D13, D16)",
            CAPABILITY_ENDPOINT,
        )
    return None


# -- identity (D8, D16) ----------------------------------------------------------------


@dataclass(frozen=True)
class RuntimeIdentity:
    """What the runtime reported about itself in one ``version`` and ``info`` read.

    ``product`` is what it is, from its report's shape and fields, never
    from its name; ``store`` the fields of :data:`STORE_FIELDS`, in order,
    as ``(field, value)`` pairs. ``executable`` is the CLI that was called.
    """

    product: RuntimeProduct
    version: str
    api_version: str
    rootless: bool | None
    endpoint: str
    store: tuple[tuple[str, str], ...]
    executable: str
    named: RuntimeProduct

    @property
    def name(self) -> str:
        return PRODUCT_NAMES[self.product]

    @property
    def store_text(self) -> str:
        """The store as one line: what a journal is bound to (D6 item 9)."""
        fields = ";".join(f"{key}={value}" for key, value in self.store)
        return f"{self.product.value}:{fields}"

    @property
    def behind_another_name(self) -> bool:
        """Whether the CLI's name is the other runtime's (``podman-docker``)."""
        return self.named is not self.product

    def report(self) -> RuntimeReport:
        """The runtime check's report (ADR 0013 D16), for the lifecycle and the pin."""
        return RuntimeReport(
            BackendKind.CONTAINER,
            product=self.name,
            version=self.version,
            rootless=self.rootless,
            endpoint=self.endpoint,
            store=self.store_text,
        )

    def describe(self) -> str:
        """One line for a diagnostic: product, version, rootlessness and the CLI's name."""
        rootless = {True: "rootless", False: "rootful", None: "rootlessness unknown"}[self.rootless]
        through = (
            f", reached through {self.named.value} ({self.executable})"
            if self.behind_another_name
            else ""
        )
        return f"{self.name} {self.version} ({rootless}, API {self.api_version}{through})"


def _field(data: object, dotted: str) -> object:
    for part in dotted.split("."):
        if not isinstance(data, dict) or part not in data:
            return None
        data = data[part]
    return data


def _text(value: object) -> str:
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        text = str(value)
        if text and len(text) <= _MAX_FIELD and "\n" not in text and "\0" not in text:
            return text
    return ""


def _shape(version: object, info: object) -> RuntimeProduct | None:
    """Which runtime the two reports are, by their own fields; ``None`` for neither or both."""
    podman = isinstance(_field(info, "host"), dict) and isinstance(_field(info, "store"), dict)
    docker = bool(_text(_field(info, "ID"))) and bool(_text(_field(info, "DockerRootDir")))
    components = _field(version, "Server.Components")
    engine = isinstance(components, list) and any(
        isinstance(item, dict) and item.get("Name") == "Engine" for item in components
    )
    if podman and not docker and not engine:
        return RuntimeProduct.PODMAN
    if docker and engine and not podman:
        return RuntimeProduct.DOCKER
    return None


def parse_identity(
    version_json: str,
    info_json: str,
    *,
    named: RuntimeProduct,
    executable: str,
    endpoint: str,
) -> RuntimeIdentity:
    """The runtime's identity from its own ``version`` and ``info`` JSON (D8, D16).

    Refused as unverified when either does not parse, when the reports are
    neither runtime's shape or both, when a field the identity needs is
    missing, for Docker Desktop, and for a Podman whose service is remote.
    """
    try:
        version = json.loads(version_json)
        info = json.loads(info_json)
    except (ValueError, RecursionError):
        raise _refused(
            f"{executable} gave a version or info report that is not JSON, so its identity is "
            "unverified; nothing was saved or launched",
            CAPABILITY_IDENTITY,
        ) from None
    product = _shape(version, info)
    if product is None:
        raise _refused(
            f"{executable}'s own version and info reports are neither Docker Engine's nor "
            "Podman's, so its identity is unverified; its name is never taken as proof "
            "(ADR 0013 D8)",
            CAPABILITY_IDENTITY,
        )
    if product is RuntimeProduct.DOCKER:
        version_text = _text(_field(version, "Server.Version")) or _text(
            _field(info, "ServerVersion")
        )
        api = _text(_field(version, "Server.ApiVersion"))
        options = _field(info, "SecurityOptions")
        rootless: bool | None = (
            any(isinstance(opt, str) and "name=rootless" in opt.split(",") for opt in options)
            if isinstance(options, list)
            else None
        )
        system = _text(_field(info, "OperatingSystem"))
        if "docker desktop" in system.lower():
            raise _refused(
                "the Docker Engine reports Docker Desktop, whose engine runs in a VM as root; "
                "it is refused (ADR 0013 D10, D16)",
                CAPABILITY_ENDPOINT,
            )
    else:
        version_text = _text(_field(version, "Client.Version")) or _text(
            _field(info, "version.Version")
        )
        api = _text(_field(version, "Client.APIVersion")) or _text(
            _field(info, "version.APIVersion")
        )
        raw = _field(info, "host.security.rootless")
        rootless = raw if isinstance(raw, bool) else None
        if _field(info, "host.serviceIsRemote") is True:
            raise _refused(
                "Podman reports a remote service, so its containers are another machine's; "
                "the container backend drives only the local runtime (ADR 0013 D16)",
                CAPABILITY_ENDPOINT,
            )
    store: list[tuple[str, str]] = []
    for key in STORE_FIELDS[product]:
        value = _text(_field(info, key))
        if not value:
            raise _refused(
                f"{PRODUCT_NAMES[product]}'s info report has no {key}, which identifies its "
                "store, so a journal could not be bound to it (ADR 0013 D16)",
                CAPABILITY_IDENTITY,
            )
        store.append((key, value))
    if not version_text:
        raise _refused(
            f"{PRODUCT_NAMES[product]}'s version report names no version", CAPABILITY_IDENTITY
        )
    return RuntimeIdentity(
        product=product,
        version=version_text,
        api_version=api,
        rootless=rootless,
        endpoint=endpoint,
        store=tuple(store),
        executable=executable,
        named=named,
    )


# -- the client (D13) ------------------------------------------------------------------


class RuntimeClient:
    """The controller's calls of one runtime CLI: hardened, allow-listed, empty configuration.

    ``choice`` is :func:`detect`'s; ``environ`` the controller's
    environment, of which only :data:`CLIENT_ENV_ALLOWLIST` and an admitted
    endpoint variable are passed. The endpoint is checked here, before any
    call. ``runner`` is the executor (a test's fake runtime is a real
    executable it runs).
    """

    def __init__(
        self,
        choice: RuntimeChoice,
        environ: Mapping[str, str],
        *,
        runner: Runner | None = None,
    ) -> None:
        refusal = endpoint_refusal(environ)
        if refusal is not None:
            raise refusal
        self.choice = choice
        self._runner = runner or execute
        base = {name: environ[name] for name in CLIENT_ENV_ALLOWLIST if name in environ}
        for name in ENDPOINT_VARIABLES:
            if name in environ:
                base[name] = environ[name]
        self._base = base
        if choice.named is RuntimeProduct.DOCKER:
            self.endpoint = environ.get("DOCKER_HOST", DOCKER_DEFAULT_ENDPOINT)
        else:
            self.endpoint = environ.get("CONTAINER_HOST", PODMAN_LOCAL_ENDPOINT)

    @property
    def executable(self) -> str:
        return self.choice.executable

    def reserves(self, name: str) -> bool:
        """Whether ``name`` is the client's own: never a launch's variable on an exec."""
        return name in self._base or name in ("DOCKER_CONFIG", "REGISTRY_AUTH_FILE")

    def environment(
        self, config_dir: str, extra: Mapping[str, str] | None = None
    ) -> dict[str, str]:
        """A call's whole environment: the allow-listed base, the empty configuration, ``extra``.

        ``extra`` is a create's projected values (``sandbox_container.client_environment``
        adds them to this base); a name of the base is never overwritten.
        """
        env = dict(self._base)
        env["DOCKER_CONFIG"] = config_dir
        env["REGISTRY_AUTH_FILE"] = os.path.join(config_dir, "auth.json")
        for name, value in (extra or {}).items():
            if name in env:
                raise _refused(f"the runtime client's environment already holds {name}")
            env[name] = value
        return env

    def run(
        self,
        args: Sequence[str],
        *,
        timeout_seconds: int = REPORT_TIMEOUT_SECONDS,
        max_output_bytes: int = REPORT_MAX_OUTPUT_BYTES,
        stdin_data: bytes | None = None,
        stdin_path: str | None = None,
        on_chunk: Callable[[str, bytes], None] | None = None,
        extra_env: Callable[[dict[str, str]], dict[str, str]] | None = None,
        stop: object = None,
    ) -> ExecutionResult:
        """One runtime CLI call, ``args`` after the executable, in a fresh empty configuration.

        The configuration directory is made for this call alone, mode
        ``0700``, and removed after it whatever the call wrote there.
        ``extra_env`` completes the environment (a create's projection, by
        :func:`autoforge.sandbox_container.client_environment`).
        """
        for word in args:
            if not isinstance(word, str) or "\0" in word:
                raise _refused(f"a runtime argument is not a string: {word!r}")
        config_dir = tempfile.mkdtemp(prefix="autoforge-runtime-client-")
        try:
            env = self.environment(config_dir)
            if extra_env is not None:
                env = extra_env(env)
            request = ExecutionRequest(
                command=[self.executable, *args],
                cwd=config_dir,
                env=env,
                env_allowlist=(),
                timeout_seconds=timeout_seconds,
                max_output_bytes=max_output_bytes,
                stdin_data=stdin_data,
                stdin_path=stdin_path,
                on_chunk=on_chunk,
            )
            if stop is not None:
                request.stop = stop  # type: ignore[assignment]
            return self._runner(request)
        finally:
            shutil.rmtree(config_dir, ignore_errors=True)

    def _report(self, what: Literal["version", "info"]) -> str:
        res = self.run([what, "--format", _JSON_FORMAT])
        if res.exit_code != 0 or res.timed_out:
            ended = "timed out" if res.timed_out else f"exited {res.exit_code}"
            detail = redact((res.stderr or res.stdout).strip())[:300]
            raise _refused(
                f"{self.executable} {what} {ended}, so the runtime is unverified "
                f"({self.choice.how}); the other runtime is never tried instead"
                + (f": {detail}" if detail else ""),
            )
        if res.stdout_truncated:
            raise _refused(
                f"{self.executable} {what}'s report is over its bound", CAPABILITY_IDENTITY
            )
        return res.stdout

    def identity(self) -> RuntimeIdentity:
        """The runtime's identity, read from it now (D6 item 10): never cached here."""
        version = self._report("version")
        info = self._report("info")
        return parse_identity(
            version,
            info,
            named=self.choice.named,
            executable=self.executable,
            endpoint=self.endpoint,
        )
