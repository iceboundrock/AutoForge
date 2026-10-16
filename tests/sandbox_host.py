"""What this host provides for the outer sandbox's real backend, measured (#169).

Whether a host can enforce is measured, never assumed, the hosted CI runner
included: ``python -m tests.sandbox_host`` (the first command of ``make
test-sandbox-measured``, which the ``sandbox`` job of
``.github/workflows/ci.yml`` runs) records

- the platform, the kernel and the distribution;
- which ``bwrap`` is on ``PATH``, its version, and whether it can mount an
  overlay (``--overlay``, bubblewrap 0.11 or later), which the backend needs;
- the user-namespace policy: the sysctls that restrict unprivileged user
  namespaces (Debian's ``kernel.unprivileged_userns_clone``, Ubuntu's
  AppArmor ``kernel.apparmor_restrict_unprivileged_userns``,
  ``user.max_user_namespaces``) and whether an unprivileged ``bwrap`` gets
  new user, PID and IPC namespaces and a new procfs;
- the kernel's Landlock ABI (6, Linux 6.12, for the abstract-socket scope),

prints it, and appends it to ``$GITHUB_STEP_SUMMARY`` when that is set, so
each CI run keeps what its runner measured.

The measurement is deliberately independent of :mod:`autoforge`: the
real-backend tests decide with the backend's own probe, and under
``AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto``
(:func:`tests.conftest.real_sandbox_backend`) a host this module measures
capable fails them instead of skipping, so a probe that wrongly says
"unsupported" cannot turn a capable runner's coverage into a skip.

Nothing is written but the summary. ``bwrap --version``, ``bwrap --help``
and one trivial command in the new namespaces are the only processes run.
"""

from __future__ import annotations

import ctypes
import os
import platform
import shutil
import subprocess
import sys
from dataclasses import dataclass

# The Landlock ABI with the abstract-socket scope (Linux 6.12). Kept here
# rather than imported, so that the measurement does not share the probe's
# mistakes.
MIN_LANDLOCK_ABI = 6
# landlock_create_ruleset, the same number on every architecture, and its
# flag that asks for the ABI version.
_SYS_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_CREATE_RULESET_VERSION = 1

# The sysctls that decide whether an unprivileged process may create a user
# namespace, by their /proc/sys path.
USERNS_SYSCTLS = (
    "kernel/unprivileged_userns_clone",
    "kernel/apparmor_restrict_unprivileged_userns",
    "user/max_user_namespaces",
)
ABSENT = "absent"

_TIMEOUT_SECONDS = 30
_SHOWN_CHARS = 200


@dataclass(frozen=True)
class HostReport:
    """What one measurement found; :attr:`missing` is what the backend would lack."""

    platform: str
    kernel: str
    distribution: str
    bwrap: str
    bwrap_version: str
    bwrap_overlay: bool
    sysctls: tuple[tuple[str, str], ...]
    # "" when the unprivileged namespace launch succeeded, else why it did not.
    namespaces: str
    landlock_abi: int

    @property
    def missing(self) -> list[str]:
        """What this host lacks for an enforced launch, in the order a launch needs it."""
        if not self.platform.startswith("linux"):
            return ["a Linux kernel"]
        found: list[str] = []
        if not self.bwrap:
            found.append("bwrap on PATH")
        else:
            if not self.bwrap_overlay:
                found.append(
                    f"a bwrap that can mount an overlay (bubblewrap {self.bwrap_version or '?'} "
                    "has no --overlay; 0.11 or later has)"
                )
            if self.namespaces:
                found.append(
                    "new user, PID and IPC namespaces and a new procfs for an unprivileged "
                    f"bwrap ({self.namespaces})"
                )
        if self.landlock_abi < MIN_LANDLOCK_ABI:
            found.append(
                f"Landlock ABI {MIN_LANDLOCK_ABI} (the kernel's is "
                f"{self.landlock_abi or 'unavailable'})"
            )
        return found

    @property
    def capable(self) -> bool:
        return not self.missing

    def verdict(self) -> str:
        if self.capable:
            return "this host can enforce the outer sandbox"
        return "this host cannot enforce the outer sandbox: it lacks " + "; ".join(self.missing)

    def rows(self) -> list[tuple[str, str]]:
        bwrap = (
            f"{self.bwrap}, bubblewrap {self.bwrap_version or '(version unknown)'}, "
            f"--overlay {'yes' if self.bwrap_overlay else 'no'}"
            if self.bwrap
            else "not found on PATH"
        )
        if not self.bwrap:
            namespaces = "not measured (no bwrap)"
        elif self.namespaces:
            namespaces = f"refused: {self.namespaces}"
        else:
            namespaces = "granted"
        return [
            ("platform", f"{self.platform}, kernel {self.kernel}, {self.distribution}"),
            ("bwrap", bwrap),
            *((f"sysctl {name.replace('/', '.')}", value) for name, value in self.sysctls),
            ("unprivileged bwrap: user, PID and IPC namespaces, new procfs", namespaces),
            ("Landlock ABI", str(self.landlock_abi or "unavailable")),
        ]

    def markdown(self) -> str:
        lines = [
            "### Outer sandbox: what this host provides (tests/sandbox_host.py)",
            "",
            "| measured | result |",
            "|---|---|",
            *(f"| {_cell(name)} | {_cell(value)} |" for name, value in self.rows()),
            "",
        ]
        verdict = self.verdict()
        verdict = verdict[:1].upper() + verdict[1:]
        if self.capable:
            lines.append(
                f"**{verdict}.** The real-backend tests are required here "
                "(`AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto`): one that would skip fails."
            )
        else:
            lines.append(
                f"**{verdict}.** The real-backend tests skip with the backend probe's "
                "reason; `make test-sandbox` runs them on a capable host "
                '(docs/development.md, "The outer sandbox").'
            )
        return "\n".join(lines) + "\n"


def _cell(text: str) -> str:
    return text.replace("|", "\\|").replace("\n", " ")


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return (lines[0] if lines else "")[:_SHOWN_CHARS]


def _run(argv: list[str]) -> subprocess.CompletedProcess[str] | str:
    """``argv``'s result, or why it could not be run."""
    try:
        return subprocess.run(
            argv,
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
            env={"PATH": os.environ.get("PATH", os.defpath)},
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return f"{type(exc).__name__}: {exc}"


def _sysctl(name: str) -> str:
    try:
        with open(f"/proc/sys/{name}", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ABSENT


def _distribution() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            for line in fh:
                key, _, value = line.strip().partition("=")
                if key == "PRETTY_NAME":
                    return value.strip("\"'")
    except OSError:
        pass
    return "unknown distribution"


def landlock_abi() -> int:
    """The kernel's Landlock ABI version; 0 when Landlock is absent, disabled or not Linux."""
    if not sys.platform.startswith("linux"):
        return 0
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        abi = libc.syscall(
            ctypes.c_long(_SYS_LANDLOCK_CREATE_RULESET),
            None,
            ctypes.c_size_t(0),
            ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION),
        )
    except (OSError, AttributeError):
        return 0
    return max(int(abi), 0)


def _namespaces(bwrap: str) -> str:
    """ "" when an unprivileged ``bwrap`` runs ``true`` in new namespaces; else why not."""
    true = shutil.which("true") or "/bin/true"
    argv = [bwrap, "--unshare-user", "--unshare-pid", "--unshare-ipc"]
    argv += ["--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", true]
    result = _run(argv)
    if isinstance(result, str):
        return result
    if result.returncode == 0:
        return ""
    return _first_line(result.stderr) or f"exited {result.returncode}"


def measure() -> HostReport:
    """Measure this host (module docstring)."""
    bwrap = shutil.which("bwrap") or ""
    version, overlay, namespaces = "", False, ""
    if bwrap:
        shown = _run([bwrap, "--version"])
        if not isinstance(shown, str):
            words = shown.stdout.split()
            version = " ".join(words[-1:])
        helped = _run([bwrap, "--help"])
        if not isinstance(helped, str):
            overlay = "--overlay" in helped.stdout + helped.stderr
        namespaces = _namespaces(bwrap)
    return HostReport(
        platform=sys.platform,
        kernel=platform.release(),
        distribution=_distribution(),
        bwrap=bwrap,
        bwrap_version=version,
        bwrap_overlay=overlay,
        sysctls=tuple((name, _sysctl(name)) for name in USERNS_SYSCTLS),
        namespaces=namespaces,
        landlock_abi=landlock_abi(),
    )


def main() -> int:
    """Print the measurement, and append it to the CI job's summary when there is one."""
    text = measure().markdown()
    print(text, end="")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
