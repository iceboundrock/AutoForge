"""The host measurement that decides whether the real backend is required (#169, PR #231 R1-F1).

``tests/sandbox_host.py`` measures what a host provides for the outer
sandbox's real backend, independently of the backend's own probe; the
hosted ``sandbox`` CI job records it and runs the real-backend modules with
``AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto``, under which
:func:`tests.conftest.real_sandbox_backend` fails rather than skips on a
host measured capable. Here ``bwrap`` is a fake on ``PATH`` and the Landlock
ABI a stand-in, so nothing depends on this host's own capability.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from autoforge import sandbox_bwrap
from autoforge.sandbox_bwrap import Capability, ProbeResult
from tests import conftest, sandbox_host
from tests.conftest import REQUIRE_SANDBOX_BACKEND, real_sandbox_backend

OLD_BWRAP = """#!/bin/sh
case "$1" in
  --version) echo "bubblewrap 0.9.0" ;;
  --help) echo "usage: bwrap [OPTIONS...] [--] COMMAND [ARGS...]"; echo "    --bind SRC DEST" ;;
  *) echo "bwrap: setting up uid map: Permission denied" >&2; exit 1 ;;
esac
"""

CAPABLE_BWRAP = """#!/bin/sh
case "$1" in
  --version) echo "bubblewrap 0.12.0" ;;
  --help) echo "    --overlay RWSRC WORKDIR DEST" ;;
  *) exit 0 ;;
esac
"""


def _path_with(tmp_path: Path, monkeypatch, script: str | None) -> Path:
    """A ``PATH`` whose only ``bwrap`` is ``script`` (none when ``None``), and ``true``."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    if script is not None:
        fake = bin_dir / "bwrap"
        fake.write_text(script)
        fake.chmod(0o755)
    true = bin_dir / "true"
    true.write_text("#!/bin/sh\nexit 0\n")
    true.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir))
    return bin_dir


def _report(**changes) -> sandbox_host.HostReport:
    fields = {
        "platform": "linux",
        "kernel": "6.12.0-test",
        "distribution": "Test Linux 1",
        "bwrap": "/usr/bin/bwrap",
        "bwrap_version": "0.12.0",
        "bwrap_overlay": True,
        "sysctls": (("kernel/apparmor_restrict_unprivileged_userns", "0"),),
        "namespaces": "",
        "landlock_abi": 6,
    }
    fields.update(changes)
    return sandbox_host.HostReport(**fields)


def test_an_old_bwrap_without_namespaces_or_landlock_is_measured_incapable(tmp_path, monkeypatch):
    bin_dir = _path_with(tmp_path, monkeypatch, OLD_BWRAP)
    monkeypatch.setattr(sandbox_host, "landlock_abi", lambda: 4)
    report = sandbox_host.measure()
    assert report.bwrap == str(bin_dir / "bwrap") and report.bwrap_version == "0.9.0"
    assert not report.bwrap_overlay
    assert report.namespaces == "bwrap: setting up uid map: Permission denied"
    assert not report.capable
    missing = report.missing
    assert len(missing) == 3
    assert "bubblewrap 0.9.0 has no --overlay" in missing[0]
    assert "user, PID and IPC namespaces" in missing[1] and "uid map" in missing[1]
    assert missing[2] == "Landlock ABI 6 (the kernel's is 4)"
    # Every measured fact is recorded, the sysctls included, whatever their value.
    text = report.markdown()
    for name in sandbox_host.USERNS_SYSCTLS:
        assert f"sysctl {name.replace('/', '.')}" in text
    assert "refused: bwrap: setting up uid map" in text and "| Landlock ABI | 4 |" in text
    assert "This host cannot enforce the outer sandbox" in text
    assert "`make test-sandbox`" in text


def test_a_capable_host_is_measured_capable_and_the_tests_said_required(tmp_path, monkeypatch):
    _path_with(tmp_path, monkeypatch, CAPABLE_BWRAP)
    monkeypatch.setattr(sandbox_host, "landlock_abi", lambda: 6)
    report = sandbox_host.measure()
    assert report.bwrap_version == "0.12.0" and report.bwrap_overlay
    assert report.namespaces == "" and report.capable and report.missing == []
    text = report.markdown()
    assert "| unprivileged bwrap: user, PID and IPC namespaces, new procfs | granted |" in text
    assert "This host can enforce the outer sandbox" in text
    assert "AUTOFORGE_REQUIRE_SANDBOX_BACKEND=auto" in text


def test_a_host_without_bwrap_is_measured_and_named(tmp_path, monkeypatch):
    _path_with(tmp_path, monkeypatch, None)
    monkeypatch.setattr(sandbox_host, "landlock_abi", lambda: 6)
    report = sandbox_host.measure()
    assert report.missing == ["bwrap on PATH"]
    text = report.markdown()
    assert "| bwrap | not found on PATH |" in text and "not measured (no bwrap)" in text


def test_only_linux_has_a_backend():
    assert _report(platform="darwin").missing == ["a Linux kernel"]


def test_the_measurement_is_printed_and_appended_to_the_ci_job_summary(
    tmp_path, monkeypatch, capsys
):
    report = _report(namespaces="bwrap: a | b")
    monkeypatch.setattr(sandbox_host, "measure", lambda: report)
    summary = tmp_path / "summary.md"
    summary.write_text("earlier step\n")
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    assert sandbox_host.main() == 0
    text = report.markdown()
    assert capsys.readouterr().out == text
    assert summary.read_text() == "earlier step\n" + text
    # A value cannot break the table.
    assert "refused: bwrap: a \\| b" in text


# -- AUTOFORGE_REQUIRE_SANDBOX_BACKEND, against a probe that says unsupported -------
@pytest.mark.parametrize(
    ("require", "capable", "outcome", "words"),
    [
        ("", True, "skip", "cannot provide a new procfs"),
        ("1", False, "fail", "AUTOFORGE_REQUIRE_SANDBOX_BACKEND=1, but"),
        ("auto", True, "fail", "measures that this host can enforce the outer sandbox, but"),
        ("auto", False, "skip", "measured: this host cannot enforce the outer sandbox"),
        ("yes", False, "fail", "'yes' is neither 1 nor auto"),
    ],
)
def test_the_requirement_decides_whether_an_unsupported_probe_skips_or_fails(
    tmp_path_factory, monkeypatch, require, capable, outcome, words
):
    if not sys.platform.startswith("linux"):
        pytest.skip("the backend is Linux-only")
    monkeypatch.setattr(conftest, "SYSTEM_PYTHON", sys.executable)
    monkeypatch.setattr(
        sandbox_bwrap,
        "probe",
        lambda **kwargs: ProbeResult(
            False, "bwrap: Can't mount proc on /newroot/proc", "", Capability.PROCFS
        ),
    )
    measured = _report() if capable else _report(landlock_abi=4)
    monkeypatch.setattr(sandbox_host, "measure", lambda: measured)
    if require:
        monkeypatch.setenv(REQUIRE_SANDBOX_BACKEND, require)
    else:
        monkeypatch.delenv(REQUIRE_SANDBOX_BACKEND, raising=False)
    expected = pytest.fail.Exception if outcome == "fail" else pytest.skip.Exception
    with pytest.raises(expected) as caught:
        real_sandbox_backend(tmp_path_factory)
    assert words in str(caught.value)
    assert "cannot provide a new procfs" in str(caught.value)
