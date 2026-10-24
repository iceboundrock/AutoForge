"""The PyPI publishing workflow (#295): what may start it and what each job may do.

``.github/workflows/pypi-publish.yml`` is the only path from this repository
to PyPI. These tests read it (with PyYAML, which the dev group always
installs, see ``tests/test_config.py``) and fail when

- anything but the push of a version tag, or a manual dispatch with no input,
  can start it;
- a job gets a permission beyond the one it needs: ``build`` reads the
  repository and has no ``id-token``, ``publish`` has ``id-token: write`` and
  nothing else and runs in the ``pypi`` environment;
- an action is not pinned to a full commit SHA, a secret is referenced, or a
  ``run`` script interpolates an expression instead of reading a variable;
- ``build`` stops building the artifacts once with the dist check CI runs and
  recording their digests, or ``publish`` stops re-verifying exactly those
  files before it uploads them.

PyPI's trusted publisher names the workflow's file name and the environment
(docs/releasing.md), so both are pinned here too. Nothing here runs the
workflow; GitHub does, only for a tag.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "pypi-publish.yml"
TEXT = WORKFLOW.read_text(encoding="utf-8")
DATA: dict[Any, Any] = yaml.safe_load(TEXT)
# YAML 1.1 reads the bare key `on` as the boolean true.
TRIGGERS = DATA.get("on", DATA.get(True))
JOBS: dict[str, dict[str, Any]] = DATA["jobs"]
PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40} # v\d+\.\d+\.\d+$")


def _steps(job: str) -> list[dict[str, Any]]:
    return JOBS[job]["steps"]


def _uses(job: str) -> list[str]:
    return [step["uses"].split("@", 1)[0] for step in _steps(job) if "uses" in step]


def _step(job: str, uses: str) -> dict[str, Any]:
    (step,) = [s for s in _steps(job) if s.get("uses", "").split("@", 1)[0] == uses]
    return step


def _runs(job: str) -> list[str]:
    return [step["run"] for step in _steps(job) if "run" in step]


def _ci_pythons() -> list[str]:
    for line in (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
        if line.startswith("CI_PYTHON_VERSIONS ="):
            return line.split("=", 1)[1].split()
    raise AssertionError("Makefile defines no CI_PYTHON_VERSIONS")


def test_only_a_version_tag_or_a_manual_dispatch_starts_it():
    assert TRIGGERS == {"push": {"tags": ["v*"]}, "workflow_dispatch": None}, TRIGGERS


def test_the_workflow_grants_nothing_and_each_job_states_its_own():
    assert DATA["permissions"] == {}
    assert set(JOBS) == {"build", "publish"}


def test_build_reads_the_repository_and_cannot_mint_a_token():
    build = JOBS["build"]
    assert build["permissions"] == {"contents": "read"}
    assert "environment" not in build
    assert "id-token" not in str(build)


def test_publish_alone_mints_a_token_in_the_protected_environment():
    publish = JOBS["publish"]
    assert publish["permissions"] == {"id-token": "write"}
    assert publish["needs"] == "build"
    assert publish["environment"]["name"] == "pypi"
    assert publish["environment"]["url"].startswith("https://pypi.org/project/autoforge-cli/")


def test_every_action_is_pinned_to_a_commit_sha_with_its_release_beside_it():
    lines = [
        line.strip() for line in TEXT.splitlines() if line.strip().startswith(("uses:", "- uses:"))
    ]
    assert lines, "the workflow uses no action"
    for line in lines:
        reference = line.removeprefix("- ").removeprefix("uses:").strip()
        assert PINNED.match(reference), f"not pinned to a full commit SHA and its tag: {line}"


def test_no_secret_and_no_expression_inside_a_script():
    assert "secrets." not in TEXT
    for job in JOBS:
        for script in _runs(job):
            assert "${{" not in script, f"{job}: a run script interpolates an expression: {script}"
    publish_with = _step("publish", "pypa/gh-action-pypi-publish").get("with", {})
    assert not {"user", "password", "repository-url"} & set(publish_with), publish_with


def test_build_refuses_any_ref_but_a_version_tag_before_anything_else():
    first = _steps("build")[0]
    assert first["if"] == "github.ref_type != 'tag' || !startsWith(github.ref_name, 'v')"
    assert "exit 1" in first["run"]


def test_build_checks_out_without_keeping_the_token():
    assert _uses("build")[0] == "actions/checkout"
    assert _step("build", "actions/checkout")["with"] == {"persist-credentials": False}


def test_build_builds_once_with_the_dist_check_ci_runs_and_records_the_digests():
    assert _uses("build") == [
        "actions/checkout",
        "astral-sh/setup-uv",
        "actions/upload-artifact",
    ]
    setup = _step("build", "astral-sh/setup-uv")["with"]
    assert setup["python-version"] in _ci_pythons()
    assert setup["enable-cache"] is False
    checks = [s for s in _runs("build") if "tests/dist_check.py" in s]
    assert len(checks) == 1, "the artifacts are built exactly once"
    (check,) = checks
    python = setup["python-version"]
    assert check.startswith(f"uv run --no-project --python {python} python -I tests/dist_check.py ")
    assert '--release-tag "${GITHUB_REF_NAME}"' in check
    assert '--out-dir "${RUNNER_TEMP}/release/dist"' in check
    assert '--sha256sums "${RUNNER_TEMP}/release/SHA256SUMS"' in check
    (dist,) = [s for s in _steps("build") if "tests/dist_check.py" in s.get("run", "")]
    outputs = JOBS["build"]["outputs"]
    assert outputs == {
        "version": f"${{{{ steps.{dist['id']}.outputs.version }}}}",
        "sha256sums": f"${{{{ steps.{dist['id']}.outputs.sha256sums }}}}",
    }
    upload = _step("build", "actions/upload-artifact")["with"]
    assert upload["name"] == "release"
    assert upload["path"].splitlines() == [
        "${{ runner.temp }}/release/dist/",
        "${{ runner.temp }}/release/SHA256SUMS",
    ]
    assert upload["if-no-files-found"] == "error"


def test_publish_reverifies_exactly_the_recorded_files_and_uploads_them_with_attestations():
    assert _uses("publish") == ["actions/download-artifact", "pypa/gh-action-pypi-publish"]
    download = _step("publish", "actions/download-artifact")["with"]
    assert download == {"name": "release", "path": "release"}
    (verify,) = [s for s in _steps("publish") if "run" in s]
    assert verify["env"] == {
        "RECORDED": "${{ needs.build.outputs.sha256sums }}",
        "VERSION": "${{ needs.build.outputs.version }}",
    }
    script = verify["run"]
    assert "cmp recorded.sha256 release/SHA256SUMS" in script
    assert "sha256sum --check --strict" in script
    assert "autoforge_cli-${VERSION}-py3-none-any.whl" in script
    assert "autoforge_cli-${VERSION}.tar.gz" in script
    steps = _steps("publish")
    assert steps.index(verify) < steps.index(_step("publish", "pypa/gh-action-pypi-publish"))
    upload = _step("publish", "pypa/gh-action-pypi-publish")["with"]
    assert upload["packages-dir"] == "release/dist/"
    assert upload["attestations"] is True
    assert "skip-existing" not in upload


def _reverify(tmp_path: Path, recorded: str, version: str) -> subprocess.CompletedProcess[str]:
    """Run the publish job's re-verify script as GitHub does (bash -e) in `tmp_path`."""
    (verify,) = [s for s in _steps("publish") if "run" in s]
    env = {"PATH": os.environ["PATH"], "RECORDED": recorded, "VERSION": version}
    return subprocess.run(
        ["bash", "-e", "-c", verify["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.mark.parametrize("trailing_newline", [False, True])
def test_the_reverify_step_accepts_exactly_the_recorded_files(
    tmp_path: Path, trailing_newline: bool
):
    """The publish job's own script, run on what the build job hands it."""
    version = "0.1.0a0"
    dist = tmp_path / "release" / "dist"
    dist.mkdir(parents=True)
    names = [f"autoforge_cli-{version}.tar.gz", f"autoforge_cli-{version}-py3-none-any.whl"]
    lines = []
    for name in names:
        (dist / name).write_bytes(name.encode())
        lines.append(f"{hashlib.sha256(name.encode()).hexdigest()}  {name}")
    sums = "".join(f"{line}\n" for line in lines)
    (tmp_path / "release" / "SHA256SUMS").write_text(sums, encoding="utf-8")
    # A job output's value, as GitHub hands it on, may or may not end in a newline.
    recorded = sums if trailing_newline else sums.rstrip("\n")
    result = _reverify(tmp_path, recorded, version)
    assert result.returncode == 0, result.stdout + result.stderr

    # A file changed after build recorded it.
    (dist / names[1]).write_bytes(b"something else")
    assert _reverify(tmp_path, recorded, version).returncode != 0
    (dist / names[1]).write_bytes(names[1].encode())
    # A file build did not record.
    (dist / "extra-1.0-py3-none-any.whl").write_bytes(b"x")
    assert _reverify(tmp_path, recorded, version).returncode != 0
    (dist / "extra-1.0-py3-none-any.whl").unlink()
    # Another version than the one build checked against the tag.
    assert _reverify(tmp_path, recorded, "0.1.0a1").returncode != 0
    # An artifact SHA256SUMS that is not what build recorded.
    (tmp_path / "release" / "SHA256SUMS").write_text(lines[0] + "\n", encoding="utf-8")
    assert _reverify(tmp_path, recorded, version).returncode != 0
    # Nothing recorded at all.
    (tmp_path / "release" / "SHA256SUMS").write_text(sums, encoding="utf-8")
    assert _reverify(tmp_path, recorded, version).returncode == 0
    assert _reverify(tmp_path, "", version).returncode != 0
