"""What AutoForge hands a process it launches outside itself (#165).

Agents, LOCAL validation commands and pre-merge verification commands are
untrusted: an agent follows whatever a prompt injection in project data
tells it, and a repository-defined command is the PR's own code. Every
externally visible write of a run is the controller's (ADR 0004), so none
of these processes may hold a GitHub or git mutation credential through
anything AutoForge controls. This module is that boundary, provider-neutral
and with no workflow semantics; the engine applies it to all three kinds
of launch the same way.

- **Names that are never inherited** (:data:`FORBIDDEN_ENV_NAMES`,
  :data:`FORBIDDEN_ENV_PREFIXES`). gh's tokens and configuration location,
  the programs git and ssh run to obtain a credential, the SSH agent
  socket, git configuration taken from the environment, the session bus a
  desktop keyring answers on, where curl and wget find the
  configuration file they read on every request, ``NETRC``, the
  ``.netrc`` curl (8.16 and later), Python's ``requests`` and Go read
  instead of ``~/.netrc``, and where a shell finds a startup file
  (``BASH_ENV``, ``ENV``, ``ZDOTDIR``).
  :func:`refuse_forbidden_env` refuses an allow-list entry that names one,
  or a ``*`` prefix that covers one, at config load
  (``execution.env_allowlist`` / ``env_allowlist_extra``) and for every
  provider adapter's own names, naming the variable and the rule, never a
  value.
- **What the controller sets instead** (:func:`isolated_overrides`), over
  the allow-listed environment, for one launch:

  - ``GH_CONFIG_DIR`` names an empty directory the controller created for
    the launch, so the child's ``gh`` finds no ``hosts.yml``;
  - ``GH_TOKEN`` and ``GH_ENTERPRISE_TOKEN`` carry
    :data:`GH_PLACEHOLDER_TOKEN`, which is not a credential: with no token
    in its environment or configuration, ``gh`` (2.40 and later) falls
    back to the active login it keeps in the system keyring, which every
    process of the user can reach on macOS. A token in the environment is
    used first and the keyring is never asked, and GitHub refuses this one;
  - ``GIT_CONFIG_GLOBAL`` names a configuration file the controller wrote
    for the launch, holding only the author and committer identity copied
    from the operator's global configuration, so the operator's
    ``~/.gitconfig`` (credential helpers, ``url.*.insteadOf``, extra
    headers) is not read, and ``git config --global`` writes land there;
  - credential helpers, the askpass program and ``http.extraHeader`` are
    reset and ``core.sshCommand`` is a hardened ``ssh`` at git's command
    scope (``GIT_CONFIG_COUNT``), which no configuration file, the
    repository's included, can override; ``GIT_TERMINAL_PROMPT=0``. The
    ``ssh`` reads no configuration file and offers no identity file and no
    agent, so git over SSH cannot pick up the operator's default keys;
  - ``CURL_HOME`` names a directory holding an empty ``.curlrc``, and
    ``WGETRC`` (wget 1.x) and ``WGET2RC`` (wget2) an empty file. curl reads
    the first default configuration file it finds, ``$CURL_HOME/.curlrc``
    before the one in ``$XDG_CONFIG_HOME`` (``curlrc`` in curl 8.18,
    ``.curlrc`` in 8.5), ``~/.curlrc`` and the home of the password
    database, and wget reads the file its variable names instead of
    ``~/.wgetrc`` or ``~/.config/wget/wget2rc``; either file can add an
    ``Authorization`` header to every request, so a plain ``curl`` or
    ``wget`` of the child's would otherwise send the operator's token;
  - ``ZDOTDIR`` names an empty directory, where every zsh of the launch
    looks for ``.zshenv``, ``.zprofile``, ``.zshrc`` and ``.zlogin``
    instead of ``HOME``: zsh reads ``.zshenv`` on every start, ``zsh -c``
    included, and an ``export GH_TOKEN=...`` there would replace the
    placeholder for everything the shell runs;
  - ``SHELL`` names a shell that reads no startup file of the operator's to
    run a command (:func:`child_shell`): the operator's own when it is one
    of :data:`POSIX_SHELLS`, else ``bash`` (``/bin/sh`` without one). The
    agent CLIs run their shell tool with it (OpenCode as ``$SHELL -c``,
    Claude Code when it is bash or zsh), and so do the tools that start
    ``$SHELL``; fish and the csh family read their configuration on every
    start.

  The directory is removed when the launch ends.
- **A preflight before every launch** (:func:`preflight`): the git
  configuration the child would read (system, repository and worktree
  scope) must not hold a credential helper in repository or worktree scope
  (the shared common dir), a URL with an embedded credential (a remote, an
  ``insteadOf`` base) or an ``http.extraHeader``, which the command-scope
  reset cannot reach when it is set for a URL; ``~/.netrc`` must have no
  word that a netrc reader may take for a GitHub machine or a ``default``
  entry, because git's HTTPS transport reads it through ``HOME``; and the
  startup files of a login shell of the launch's must leave its variables
  alone (:func:`shell_startup_problems`). ``bash -l`` and ``sh -l`` read
  ``~/.bash_profile``, ``~/.profile`` and ``/etc/profile`` through ``HOME``,
  which no variable redirects, so each shell a launch may start (its
  ``SHELL``, ``sh``, ``bash`` and ``zsh``) is run once as a login shell in
  the launch's environment and reports, by name only, every variable of the
  isolation its startup files changed. A refusal is a
  :class:`ConfigurationError` that names the key and scope, the file or the
  shell and variable, never a value; nothing is launched.

What this does not do (docs/agent-guides/github-safety.md, "Threat
model"): it hides nothing from a process that deliberately opens a
credential file, a keychain or a socket by absolute path, or runs ``ssh``
or a credential helper of its own choosing; that is the outer sandbox's
(ADR 0005). ``HOME`` is still passed: the agent CLIs keep their own sign-in
there. So an interactive ``bash`` still reads ``~/.bashrc``, Claude Code
still sources ``~/.zshrc`` or ``~/.bashrc`` itself to snapshot the
functions and aliases (not the variables) its Bash tool runs with, and a
fish or csh-family shell started by name still reads its configuration;
none of them is started by a launch through ``SHELL``.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .errors import ConfigurationError, ExecutionError
from .executor import ExecutionRequest, ExecutionResult, execute
from .git_transport import local_git_request
from .redaction import credential_classes, redact

Runner = Callable[[ExecutionRequest], ExecutionResult]

_GH = "a GitHub CLI token or configuration location"
_PROGRAM = "a program git or ssh runs to obtain a credential or to connect"
_SSH_AGENT = "the SSH agent socket"
_GIT_CONFIG = "git configuration taken from the environment, which can name a credential helper"
_SESSION_BUS = "the session bus a desktop keyring is reached through"
_HTTP_CONFIG = (
    "where an HTTP client finds the configuration file it reads on every request, "
    "which can add a credential header"
)
_NETRC = "where an HTTP client finds the .netrc file it takes a password from"
_SHELL_STARTUP = (
    "where a shell finds a startup file it runs before a command, which can export a credential"
)

# Every variable an agent or a repository command must never inherit from
# the operator's environment, and the rule it falls under. Some of them are
# set by the controller itself (:func:`child_overrides`), to values that
# carry no credential; none is ever taken from the operator.
FORBIDDEN_ENV_NAMES: dict[str, str] = {
    "GH_TOKEN": _GH,
    "GITHUB_TOKEN": _GH,
    "GH_ENTERPRISE_TOKEN": _GH,
    "GITHUB_ENTERPRISE_TOKEN": _GH,
    "GH_HOST": _GH,
    "GH_CONFIG_DIR": _GH,
    "GIT_ASKPASS": _PROGRAM,
    "SSH_ASKPASS": _PROGRAM,
    "GIT_SSH": _PROGRAM,
    "GIT_SSH_COMMAND": _PROGRAM,
    "SSH_AUTH_SOCK": _SSH_AGENT,
    "GIT_CONFIG_GLOBAL": _GIT_CONFIG,
    "GIT_CONFIG_SYSTEM": _GIT_CONFIG,
    "GIT_CONFIG_COUNT": _GIT_CONFIG,
    "GIT_CONFIG_PARAMETERS": _GIT_CONFIG,
    "XDG_RUNTIME_DIR": _SESSION_BUS,
    "DBUS_SESSION_BUS_ADDRESS": _SESSION_BUS,
    "CURL_HOME": _HTTP_CONFIG,
    "WGETRC": _HTTP_CONFIG,
    "WGET2RC": _HTTP_CONFIG,
    "NETRC": _NETRC,
    "BASH_ENV": _SHELL_STARTUP,
    "ENV": _SHELL_STARTUP,
    "ZDOTDIR": _SHELL_STARTUP,
}
FORBIDDEN_ENV_PREFIXES: dict[str, str] = {
    "GIT_CONFIG_KEY_": _GIT_CONFIG,
    "GIT_CONFIG_VALUE_": _GIT_CONFIG,
}

# What the child's ``gh`` finds as its token. Not a credential, and not
# shaped like one (the redactor leaves it alone); GitHub answers 401.
GH_PLACEHOLDER_TOKEN = "autoforge-no-github-credential"

# The ``ssh`` git runs for a child: no configuration file (``-F /dev/null``
# also skips the system one), no identity file (``IdentityFile=none``; the
# defaults under ``~/.ssh`` are found through the password database, not
# ``HOME``, so only this keeps them out), no agent and no shared connection,
# and no prompt.
SSH_COMMAND = (
    "ssh -F /dev/null -o IdentityFile=none -o IdentitiesOnly=yes -o IdentityAgent=none "
    "-o ControlPath=none -o BatchMode=yes"
)

# Set at git's command scope (``GIT_CONFIG_COUNT``), which git reads after
# every configuration file. An empty ``credential.helper`` clears the helper
# list, URL-specific helpers included (git collects every matching helper in
# file order); an empty ``http.extraHeader`` clears the generic headers only,
# so a URL-specific one is refused by the preflight instead.
GIT_COMMAND_SCOPE: tuple[tuple[str, str], ...] = (
    ("credential.helper", ""),
    ("core.askPass", ""),
    ("core.sshCommand", SSH_COMMAND),
    ("http.extraHeader", ""),
)

# The identity keys copied from the operator's global configuration into the
# child's: git's own fallbacks for the author and the committer.
IDENTITY_KEYS = (
    "user.name",
    "user.email",
    "author.name",
    "author.email",
    "committer.name",
    "committer.email",
)

# The scopes of the configuration a child's git still reads: the operator's
# global configuration is replaced (``GIT_CONFIG_GLOBAL``) and the command
# scope is the controller's own.
CHILD_SCOPES = ("system", "local", "worktree")
# The scopes that live in the repository (the shared common dir and the
# worktree's own configuration), where a credential helper is refused.
REPOSITORY_SCOPES = ("local", "worktree")

# The machines of a ``.netrc`` entry git's HTTPS transport or ``curl -n``
# would use for GitHub (a trailing dot is the same host); ``default``
# matches every host.
_GITHUB_HOST_RE = re.compile(r"(?i)^(?:[a-z0-9-]+\.)*github\.com\.?$")
# What separates two words of a ``.netrc`` for some reader: whitespace, and
# the ``"`` that curl, wget and Python's ``netrc`` quote a word with.
_NETRC_WORD_BREAK = re.compile(r'[\s"]+')
NETRC_FILES = (".netrc", "_netrc")
MAX_NETRC_BYTES = 1024 * 1024

PREFLIGHT_TIMEOUT_SECONDS = 60

# The shells a launch keeps as its ``SHELL`` (by file name): POSIX shells
# that run a command (``-c``, a script) without reading a startup file,
# given that ``BASH_ENV`` and ``ENV`` never reach them and ``ZDOTDIR`` is the
# controller's. What their login startup files do is probed instead
# (:func:`shell_startup_problems`). fish, the csh family and the rest read
# their configuration on every start, through ``HOME`` or
# ``XDG_CONFIG_HOME``, which a launch keeps.
POSIX_SHELLS = ("sh", "bash", "zsh", "dash", "ksh", "mksh")
# The shells a launch may start by name besides its ``SHELL`` (a
# repository's script or recipe, ``bash -l`` included), each probed too.
PROBED_SHELLS = ("sh", "bash", "zsh")
# Printed by the probe before what it found, so whatever a startup file
# prints itself is never read as a finding.
SHELL_PROBE_MARKER = "<<<autoforge-shell-startup>>>"
# What the probe runs once the shell's startup files have: the controller's
# own interpreter, isolated (``-I``, no site), reading the environment the
# shell exports, which is what everything it starts inherits. It prints the
# marker, then the name of each variable of ``argv`` (a count, that many
# name and value pairs, then names) that differs or is set; never a value.
_PROBE_PROGRAM = (
    "import os, sys\n"
    "a = sys.argv[1:]\n"
    "n = int(a[0])\n"
    "pairs = a[1 : 1 + 2 * n]\n"
    f"print('\\n' + {SHELL_PROBE_MARKER!r})\n"
    "for name, value in zip(pairs[::2], pairs[1::2]):\n"
    "    if os.environ.get(name) != value:\n"
    "        print(name)\n"
    "for name in a[1 + 2 * n :]:\n"
    "    if name in os.environ:\n"
    "        print(name)\n"
)
# Withheld from every launch as defence in depth rather than because a
# route depends on them, so a startup file that sets one again is not
# refused: git offers no agent (``IdentityAgent=none`` at command scope,
# which the probe checks), gh asks no keyring while the placeholder token
# stands (checked too), and ``GH_HOST`` picks a host, not a token.
STARTUP_TOLERATED = frozenset(
    {"GH_HOST", "SSH_AUTH_SOCK", "XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"}
)


def forbidden_env_entry(entry: str) -> tuple[str, str] | None:
    """The forbidden variable allow-list ``entry`` would pass, and its rule; None if none.

    An exact name is forbidden when it is in :data:`FORBIDDEN_ENV_NAMES` or
    starts with a forbidden prefix; a ``*`` prefix (``GH_*``, ``GIT_*``, a
    bare ``*``) when it covers a forbidden name or overlaps a forbidden
    prefix.
    """
    if entry.endswith("*"):
        prefix = entry[:-1]
        for name, rule in FORBIDDEN_ENV_NAMES.items():
            if name.startswith(prefix):
                return name, rule
        for forbidden, rule in FORBIDDEN_ENV_PREFIXES.items():
            if forbidden.startswith(prefix) or prefix.startswith(forbidden):
                return f"{forbidden}*", rule
        return None
    if entry in FORBIDDEN_ENV_NAMES:
        return entry, FORBIDDEN_ENV_NAMES[entry]
    for forbidden, rule in FORBIDDEN_ENV_PREFIXES.items():
        if entry.startswith(forbidden):
            return entry, rule
    return None


def refuse_forbidden_env(entries: Iterable[str], where: str) -> None:
    """Raise :class:`ConfigurationError` for the first entry that would pass a
    forbidden variable. The error names the entry, the variable and the
    rule; it never reads or names a value."""
    for entry in entries:
        hit = forbidden_env_entry(entry)
        if hit is None:
            continue
        variable, rule = hit
        what = f"is {rule}" if variable == entry else f"covers {variable}, which is {rule}"
        raise ConfigurationError(
            f"{where}: environment entry {entry!r} {what}. No GitHub or git credential "
            "route may reach an agent or a repository-defined command, so it cannot be "
            "allow-listed; name the variables you need instead (docs/configuration.md, "
            "'Agent environment')"
        )


def child_shell(operator: str | None = None) -> str:
    """The ``SHELL`` a launch gets: ``operator`` (default: the controller's
    ``SHELL``) when it is an executable named in :data:`POSIX_SHELLS` by an
    absolute path, else the ``bash`` on ``PATH``, else ``/bin/sh``."""
    shell = os.environ.get("SHELL", "") if operator is None else operator
    if (
        os.path.isabs(shell)
        and os.path.basename(shell) in POSIX_SHELLS
        and os.path.isfile(shell)
        and os.access(shell, os.X_OK)
    ):
        return shell
    return shutil.which("bash") or "/bin/sh"


def child_overrides(
    gh_config_dir: str,
    git_config_global: str,
    curl_home: str,
    wgetrc: str,
    zdotdir: str,
    shell: str,
) -> dict[str, str]:
    """The variables the controller sets for a child, over the allow-listed ones."""
    env = {
        "GH_CONFIG_DIR": gh_config_dir,
        "GH_TOKEN": GH_PLACEHOLDER_TOKEN,
        "GH_ENTERPRISE_TOKEN": GH_PLACEHOLDER_TOKEN,
        "GIT_CONFIG_GLOBAL": git_config_global,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": str(len(GIT_COMMAND_SCOPE)),
        "CURL_HOME": curl_home,
        "WGETRC": wgetrc,
        "WGET2RC": wgetrc,
        "ZDOTDIR": zdotdir,
        "SHELL": shell,
    }
    for index, (key, value) in enumerate(GIT_COMMAND_SCOPE):
        env[f"GIT_CONFIG_KEY_{index}"] = key
        env[f"GIT_CONFIG_VALUE_{index}"] = value
    return env


def override_names() -> tuple[str, ...]:
    """The names :func:`child_overrides` sets, sorted (values vary by launch)."""
    return tuple(sorted(child_overrides("", "", "", "", "", "")))


@dataclass(frozen=True)
class LaunchIdentity:
    """The identity keys copied into a child's global git configuration.

    ``entries`` are ``(key, value)`` pairs of :data:`IDENTITY_KEYS`, as the
    operator's global configuration last sets them; empty when it sets none
    (the child's git then uses the repository's or the system's, as before).
    """

    entries: tuple[tuple[str, str], ...] = ()

    def describe(self) -> str:
        """Which keys are supplied, never their values."""
        if not self.entries:
            return "none in the operator's global git configuration"
        return ", ".join(key for key, _ in self.entries)


def _config_value(value: str) -> str | None:
    """``value`` as a quoted git configuration value; None if it cannot be one."""
    if any(ord(c) < 0x20 and c not in "\t\n" or c == "\x7f" for c in value):
        return None
    escaped = (
        value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n").replace("\t", "\\t")
    )
    return f'"{escaped}"'


def render_git_config(identity: LaunchIdentity) -> str:
    """The child's global git configuration: the identity, nothing else."""
    lines = ["# Written by AutoForge for one launch (#165): the operator's identity only."]
    sections: dict[str, list[str]] = {}
    for key, value in identity.entries:
        section, _, name = key.partition(".")
        quoted = _config_value(value)
        if quoted is not None:
            sections.setdefault(section, []).append(f"\t{name} = {quoted}")
    for section, body in sections.items():
        lines.append(f"[{section}]")
        lines.extend(body)
    return "\n".join(lines) + "\n"


# What the child's curl and wget find as their configuration file: options
# neither, so the operator's (``~/.curlrc``, ``~/.wgetrc``, their XDG
# locations) are never read. Both take ``#`` for a comment.
HTTP_CLIENT_CONFIG = "# Written by AutoForge for one launch (#165): no default options.\n"


def _write_new(path: str, text: str) -> None:
    """Create ``path`` (mode 0600, never through a link) holding ``text``."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


@contextmanager
def isolated_overrides(identity: LaunchIdentity) -> Iterator[dict[str, str]]:
    """The child's overrides, backed by a directory that exists for the launch only.

    The directory (``mkdtemp``, mode 0700, outside every repository) holds
    the empty gh configuration directory, the global git configuration,
    curl's and wget's configuration files and zsh's empty startup
    directory; it is removed when the ``with`` block ends, whatever the
    child wrote into it, so nothing one launch plants reaches the next. A
    directory that cannot be prepared is an :class:`ExecutionError`, before
    any launch.
    """
    try:
        root = tempfile.mkdtemp(prefix="autoforge-child-")
    except OSError as exc:
        raise ExecutionError(f"cannot prepare a launch's environment: {exc}") from exc
    try:
        try:
            gh_dir = os.path.join(root, "gh")
            os.mkdir(gh_dir, 0o700)
            git_config = os.path.join(root, "gitconfig")
            _write_new(git_config, render_git_config(identity))
            curl_home = os.path.join(root, "curl")
            os.mkdir(curl_home, 0o700)
            _write_new(os.path.join(curl_home, ".curlrc"), HTTP_CLIENT_CONFIG)
            wgetrc = os.path.join(root, "wgetrc")
            _write_new(wgetrc, HTTP_CLIENT_CONFIG)
            zdotdir = os.path.join(root, "zsh")
            os.mkdir(zdotdir, 0o700)
        except OSError as exc:
            raise ExecutionError(f"cannot prepare a launch's environment: {exc}") from exc
        yield child_overrides(gh_dir, git_config, curl_home, wgetrc, zdotdir, child_shell())
    finally:
        shutil.rmtree(root, ignore_errors=True)


# -- the preflight ------------------------------------------------------------


@dataclass(frozen=True)
class GitConfigEntry:
    """One entry of ``git config --list --show-scope``; ``value`` None for a bare key."""

    scope: str
    key: str
    value: str | None


def parse_git_config_listing(output: str) -> list[GitConfigEntry]:
    """Parse ``git config --list --show-scope -z``: ``scope NUL key LF value NUL``.

    Raises :class:`ExecutionError` on anything else, so an output the parser
    does not understand is never read as "no entries".
    """
    if not output:
        return []
    if not output.endswith("\0"):
        raise ExecutionError("git config --list printed an unterminated entry")
    fields = output[:-1].split("\0")
    if len(fields) % 2:
        raise ExecutionError("git config --list printed a scope without an entry")
    entries = []
    for scope, item in zip(fields[0::2], fields[1::2], strict=True):
        if scope not in ("system", "global", "local", "worktree", "command"):
            raise ExecutionError(f"git config --list printed an unknown scope {scope[:40]!r}")
        key, newline, value = item.partition("\n")
        if not key:
            raise ExecutionError("git config --list printed an entry without a key")
        entries.append(GitConfigEntry(scope, key, value if newline else None))
    return entries


def read_git_config(cwd: str, runner: Runner | None = None) -> list[GitConfigEntry]:
    """Every configuration entry git reads in ``cwd``, includes followed.

    Runs the hardened local request (no hooks, no ``GIT_*`` of the
    operator's shell) with the operator's own ``GIT_CONFIG_GLOBAL`` when it
    sets one, so the global scope is the one the operator's git reads, the
    scope the child's identity is copied from. Raises
    :class:`ConfigurationError` when the configuration cannot be read: a
    child would meet the same configuration.
    """
    env = {}
    operator_global = os.environ.get("GIT_CONFIG_GLOBAL")
    if operator_global:
        env["GIT_CONFIG_GLOBAL"] = operator_global
    request = local_git_request(
        ["config", "--list", "--show-scope", "--includes", "-z"],
        cwd=cwd,
        env=env,
        timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
    )
    try:
        result = (runner or execute)(request)
        if result.timed_out or result.exit_code != 0 or result.stdout_truncated:
            detail = redact(result.stderr.strip())[:300] or f"exit {result.exit_code}"
            raise ExecutionError(f"git config --list failed: {detail}")
        return parse_git_config_listing(result.stdout)
    except ExecutionError as exc:
        raise ConfigurationError(
            f"cannot read the git configuration a launch in {cwd} would use: {exc}"
        ) from exc


def _split_key(key: str) -> tuple[str, str, str]:
    """``section.subsection.name`` as its three parts (subsection may be empty)."""
    section, _, rest = key.partition(".")
    subsection, _, name = rest.rpartition(".")
    return section.lower(), subsection, name.lower()


def url_has_credential(url: str) -> bool:
    """Whether ``url`` embeds a credential: a password in its userinfo, or an
    HTTP(S) username shaped like a token. ``git@host:path`` and plain paths
    carry none; an unparseable URL with a userinfo is counted as carrying one."""
    if "://" not in url:
        return False
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
    except ValueError:
        return "@" in url.partition("://")[2].split("/", 1)[0]
    if "@" not in netloc:
        return False
    user, _, password = netloc.rpartition("@")[0].partition(":")
    if password:
        return True
    return parts.scheme.lower() in ("http", "https") and bool(credential_classes(unquote(user)))


def git_config_problems(entries: Iterable[GitConfigEntry]) -> list[str]:
    """What in the configuration a child reads would hand it a credential.

    Each problem names the scope and the key's shape, never a value and
    never a URL (a URL in a key may be the credential).
    """
    problems: list[str] = []
    for entry in entries:
        if entry.scope not in CHILD_SCOPES:
            continue
        section, subsection, name = _split_key(entry.key)
        where = f"{entry.scope}-scope git configuration"
        value = entry.value or ""
        if section == "credential" and name == "helper" and value:
            if entry.scope in REPOSITORY_SCOPES:
                shape = "credential.<url>.helper" if subsection else "credential.helper"
                problems.append(f"a credential helper in {where} ({shape})")
        elif section == "http" and name == "extraheader" and value:
            shape = "http.<url>.extraHeader" if subsection else "http.extraHeader"
            problems.append(f"an HTTP header for git in {where} ({shape})")
        elif section == "remote" and name in ("url", "pushurl") and url_has_credential(value):
            problems.append(
                f"a remote URL with an embedded credential in {where} "
                f"(remote.{redact(subsection)}.{name})"
            )
        elif (
            section == "url"
            and name in ("insteadof", "pushinsteadof")
            and url_has_credential(subsection)
        ):
            problems.append(
                f"a URL rewrite to a URL with an embedded credential in {where} (url.<url>.{name})"
            )
    return problems


def operator_identity(entries: Iterable[GitConfigEntry]) -> LaunchIdentity:
    """The identity keys the operator's global configuration last sets."""
    found: dict[str, str] = {}
    for entry in entries:
        key = entry.key.lower()
        if entry.scope == "global" and key in IDENTITY_KEYS and entry.value:
            found[key] = entry.value
    return LaunchIdentity(tuple((key, found[key]) for key in IDENTITY_KEYS if key in found))


def operator_home() -> str | None:
    """The ``HOME`` a child inherits (the controller's own)."""
    return os.environ.get("HOME") or None


def netrc_problems(home: str | None) -> list[str]:
    """A ``.netrc`` in ``home`` with an entry for GitHub, or one that cannot be read.

    git's HTTPS transport (libcurl), ``curl -n`` and Python's ``requests``
    read ``$HOME/.netrc``, and ``HOME`` reaches every child, so the file
    cannot be hidden from it. Only words are compared
    (:func:`_netrc_names_github`), and only the file name is reported.
    """
    if not home:
        return []
    problems = []
    for name in NETRC_FILES:
        path = Path(home) / name
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_NETRC_BYTES + 1)
        except FileNotFoundError:
            continue
        except OSError as exc:
            problems.append(f"~/{name} could not be checked ({type(exc).__name__})")
            continue
        if len(data) > MAX_NETRC_BYTES:
            problems.append(f"~/{name} is larger than {MAX_NETRC_BYTES} bytes and was not checked")
        elif _netrc_names_github(data.decode("utf-8", "replace")):
            problems.append(f"an entry for GitHub (or a default entry) in ~/{name}")
    return problems


def _netrc_names_github(text: str) -> bool:
    """Whether any word of a netrc text may be read as a GitHub machine or ``default``.

    The readers disagree on the syntax. curl (7.84 and later) and wget take
    a ``"``-quoted word with ``\\`` escapes; Python's ``netrc`` module, which
    ``requests`` reads, drops a ``\\`` before any character, quoted or not;
    curl matches ``machine`` and ``default`` in any case; and where a
    ``macdef`` body or a ``#`` comment ends, and whether a keyword counts in
    a value's position, differs between them (a ``macdef`` given as a login
    starts a macro for curl and none for Python). So no position, quoting,
    comment or macro is trusted: every word, split at whitespace and at
    ``"``, with each ``\\`` dropped, is compared without regard to case, and
    one that names a GitHub host or is ``default`` refuses the file, though
    it stands in a password, a comment or a macro body.
    """
    for word in _NETRC_WORD_BREAK.split(text):
        word = word.replace("\\", "").lower()
        if word == "default" or _GITHUB_HOST_RE.match(word):
            return True
    return False


def shell_probe_script(overrides: Mapping[str, str]) -> str:
    """A POSIX command that prints :data:`SHELL_PROBE_MARKER`, then the name
    of every variable of a launch's isolation the shell running it no longer
    exports as the controller left it.

    Run after the shell's startup files, it names each variable of
    ``overrides`` that is unset, holds another value or is no longer
    exported, and each forbidden name the controller withholds (but
    :data:`STARTUP_TOLERATED`) that is now exported. Values are compared in
    a process the shell starts and never printed.
    """
    pairs = sorted(overrides.items())
    withheld = sorted(set(FORBIDDEN_ENV_NAMES) - set(overrides) - STARTUP_TOLERATED)
    argv = [
        sys.executable or "python3",
        "-I",
        "-S",
        "-c",
        _PROBE_PROGRAM,
        str(len(pairs)),
        *(part for pair in pairs for part in pair),
        *withheld,
    ]
    return "exec " + shlex.join(argv) + "\n"


def probe_findings(stdout: str, names: Iterable[str]) -> list[str] | None:
    """The variables a probe's output names after its last marker, sorted;
    None when the marker is missing. Only ``names`` are read."""
    _, marker, findings = stdout.rpartition(f"\n{SHELL_PROBE_MARKER}\n")
    if not marker:
        return None
    known = set(names)
    return sorted({line for line in findings.splitlines() if line in known})


def launch_shells(shell: str) -> list[str]:
    """``shell`` and each of :data:`PROBED_SHELLS` on ``PATH``, once per name
    and file (``sh`` is probed apart from the ``bash`` it may be: bash run as
    ``sh`` reads ``~/.profile``, not ``~/.bash_profile``)."""
    found: list[str] = []
    seen: set[tuple[str, str]] = set()
    for path in [shell, *filter(None, (shutil.which(name) for name in PROBED_SHELLS))]:
        key = (os.path.basename(path), os.path.realpath(path))
        if key not in seen:
            seen.add(key)
            found.append(path)
    return found


def shell_startup_problems(
    cwd: str,
    env_allowlist: Iterable[str],
    runner: Runner | None = None,
    *,
    home: str | None = None,
) -> list[str]:
    """What the startup files of a launch's shells would change of its isolation.

    Each shell of :func:`launch_shells` runs :func:`shell_probe_script` once
    as a login shell (``-l -c``, which reads what ``-c`` reads and the login
    files besides), in ``cwd``, in the environment a launch with
    ``env_allowlist`` gets (``HOME`` is ``home``, default the operator's)
    and with its processes contained. zsh reads nothing of the operator's
    there (``ZDOTDIR``), so what it can find are the system-wide files;
    ``bash`` and ``sh`` read ``/etc/profile`` and ``~/.bash_profile``,
    ``~/.bash_login`` or ``~/.profile``. Each problem names the shell and
    the variables, never a value; a shell whose probe fails, times out or
    prints no marker could not be checked, which is a problem too.
    """
    problems: list[str] = []
    home = home if home is not None else operator_home()
    with isolated_overrides(LaunchIdentity()) as overrides:
        env = dict(overrides)
        if home:
            env["HOME"] = home
        script = shell_probe_script(overrides)
        names = set(overrides) | set(FORBIDDEN_ENV_NAMES)
        for shell in launch_shells(overrides["SHELL"]):
            where = f"the startup files of a login {os.path.basename(shell)} ({shell})"
            request = ExecutionRequest(
                command=[shell, "-l", "-c", script],
                cwd=cwd,
                timeout_seconds=PREFLIGHT_TIMEOUT_SECONDS,
                env_allowlist=tuple(env_allowlist),
                env=env,
                contain_orphans=True,
            )
            try:
                result = (runner or execute)(request)
            except (ExecutionError, OSError) as exc:
                problems.append(f"{where}, which could not be checked ({type(exc).__name__})")
                continue
            findings = None
            if not (result.timed_out or result.exit_code or result.stdout_truncated):
                findings = probe_findings(result.stdout, names)
            if findings is None:
                reason = (
                    f"timed out after {PREFLIGHT_TIMEOUT_SECONDS}s"
                    if result.timed_out
                    else f"exit {result.exit_code}"
                    if result.exit_code
                    else "no result"
                )
                problems.append(f"{where}, which could not be checked ({reason})")
            elif findings:
                problems.append(f"{where}, which change {', '.join(findings)}")
    return problems


def preflight(
    cwd: str,
    runner: Runner | None = None,
    *,
    home: str | None = None,
    env_allowlist: Iterable[str] | None = None,
) -> LaunchIdentity:
    """Refuse a launch in ``cwd`` that would hand the child a credential; else
    the identity to copy into its global git configuration.

    Run before anything is spawned: one ``git config``, one file read and,
    with the launch's ``env_allowlist`` (the engine and ``doctor`` always
    pass it), one login shell per shell a launch may start
    (:func:`shell_startup_problems`). A refusal is a
    :class:`ConfigurationError`: the operator fixes the configuration, and
    nothing needs recovering.
    """
    entries = read_git_config(cwd, runner)
    problems = git_config_problems(entries)
    home = home if home is not None else operator_home()
    problems += netrc_problems(home)
    if env_allowlist is not None:
        problems += shell_startup_problems(cwd, env_allowlist, runner, home=home)
    if problems:
        raise ConfigurationError(
            "refusing to launch: a process AutoForge launches would be handed a GitHub or "
            f"git credential through {'; '.join(problems)}. Remove it (doctor names it too; "
            "docs/configuration.md, 'Agent environment'), then run again"
        )
    return operator_identity(entries)


def describe_overrides() -> str:
    """The controller-set names, for ``doctor`` and the dry-run plan."""
    return ", ".join(override_names())


__all__ = [
    "FORBIDDEN_ENV_NAMES",
    "FORBIDDEN_ENV_PREFIXES",
    "GH_PLACEHOLDER_TOKEN",
    "GIT_COMMAND_SCOPE",
    "HTTP_CLIENT_CONFIG",
    "POSIX_SHELLS",
    "PROBED_SHELLS",
    "SHELL_PROBE_MARKER",
    "SSH_COMMAND",
    "STARTUP_TOLERATED",
    "GitConfigEntry",
    "LaunchIdentity",
    "child_overrides",
    "child_shell",
    "forbidden_env_entry",
    "git_config_problems",
    "isolated_overrides",
    "launch_shells",
    "netrc_problems",
    "operator_identity",
    "override_names",
    "parse_git_config_listing",
    "preflight",
    "probe_findings",
    "read_git_config",
    "refuse_forbidden_env",
    "shell_probe_script",
    "shell_startup_problems",
    "url_has_credential",
]
