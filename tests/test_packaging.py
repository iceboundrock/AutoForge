"""Packaging guards (#256): what the wheel and the sdist must carry, checked offline.

The real build, install and smoke is ``make dist-check`` (``tests/dist_check.py``),
which CI's ``dist`` job runs on every interpreter of the matrix. These tests are
the cheap half that runs in ``make check``: they read ``pyproject.toml``, the
package directory and the package's source, build nothing, and fail when

- a file in the package directory is neither declared package data nor named
  here as not shipped, so a new template cannot silently go missing from the
  wheel;
- a module reads a file relative to the package outside the reads this file
  knows (the prompt templates), so a new kind of runtime resource is noticed;
- the version in ``pyproject.toml`` and ``autoforge.__version__`` differ;
- the license, the distribution name, the command or the claimed interpreters
  drift from what #256 decided (D1 to D3) and ``docs/compatibility.md`` states.

They also run the dist check's own artifact checks on synthetic artifacts, so
that a wheel or an sdist missing a template is shown to fail it, and its
release-build half (#295): the tag check that refuses a tag other than
``v<version>`` before anything is built, and the digests it hands from
``pypi-publish.yml``'s build job to the publish job.
"""

from __future__ import annotations

import ast
import hashlib
import io
import re
import tarfile
import zipfile
from pathlib import Path

import pytest

import autoforge
from autoforge import prompts
from tests import dist_check
from tests.dist_check import CheckFailed

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE_DIR = REPO_ROOT / "src" / "autoforge"
DATA = dist_check.pyproject()
PROJECT = DATA["project"]

# Files in the package directory that are deliberately not in the wheel, and why.
NOT_SHIPPED = {
    "autoforge/AGENTS.md": "contributor instructions for this subtree, never read at runtime",
    "autoforge/CLAUDE.md": "imports AGENTS.md for Claude Code, never read at runtime",
}
# The modules that read a file relative to the package, and what they read. Each
# read must be covered by [tool.setuptools.package-data]; the tests below check
# the prompt templates, which are all of them today.
PACKAGE_RELATIVE_READS = {
    "autoforge/prompts/__init__.py": "prompts_dir(): the prompt templates",
    "autoforge/engine.py": "check_template_files(): the prompt templates",
}
# Ways to reach a package resource other than __file__; none is used today.
RESOURCE_MODULES = ("importlib.resources", "importlib_resources", "pkgutil", "pkg_resources")
# The versions the release procedure allows (docs/releasing.md), shared with the
# release build's tag check.
RELEASE_VERSION = dist_check.RELEASE_VERSION
# The copyright line #256 D1 recorded.
COPYRIGHT = "Copyright (c) 2026 Ruoshi Li"


def _ci_pythons() -> list[str]:
    for line in (REPO_ROOT / "Makefile").read_text(encoding="utf-8").splitlines():
        if line.startswith("CI_PYTHON_VERSIONS ="):
            return line.split("=", 1)[1].split()
    raise AssertionError("Makefile defines no CI_PYTHON_VERSIONS")


def test_the_distribution_is_autoforge_cli_and_the_command_and_import_stay_autoforge():
    """D2: `autoforge` on PyPI is someone else's; only the distribution is renamed."""
    assert PROJECT["name"] == "autoforge-cli"
    assert PROJECT["scripts"] == {"autoforge": "autoforge.cli:main"}
    assert DATA["tool"]["setuptools"]["packages"]["find"] == {"where": ["src"]}
    assert Path(autoforge.__file__).resolve().parent == PACKAGE_DIR
    assert dist_check.distribution_stem(PROJECT["name"], "0.1.0") == "autoforge_cli-0.1.0"


def test_the_license_is_mit_and_the_metadata_names_its_file():
    """D1: MIT, the recorded holder, as a PEP 639 expression and license file."""
    text = (REPO_ROOT / "LICENSE").read_text(encoding="utf-8")
    assert text.startswith("MIT License\n\n" + COPYRIGHT + "\n\n")
    assert "Permission is hereby granted, free of charge, to any person obtaining a copy" in text
    assert 'THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND' in text
    assert PROJECT["license"] == "MIT"
    assert PROJECT["license-files"] == ["LICENSE"]
    assert not any(c.startswith("License ::") for c in PROJECT["classifiers"]), (
        "PEP 639: no License classifier beside a license expression"
    )


def test_the_build_backend_supports_license_expressions():
    """setuptools reads `license = "MIT"` and `license-files` from 77.0 on; 76 refuses them."""
    (requirement,) = DATA["build-system"]["requires"]
    match = re.fullmatch(r"setuptools>=(\d+)(?:\.\d+)*", requirement)
    assert match, f"unexpected build requirement {requirement!r}; update this test"
    assert int(match.group(1)) >= 77
    assert DATA["build-system"]["build-backend"] == "setuptools.build_meta"


def test_the_version_has_one_value():
    """pyproject.toml and `autoforge.__version__` agree, in the release scheme's form."""
    assert PROJECT["version"] == autoforge.__version__
    assert RELEASE_VERSION.fullmatch(PROJECT["version"]), (
        f"{PROJECT['version']!r} is not X.Y.Z[aN|bN|rcN] (docs/releasing.md)"
    )


def test_every_file_in_the_package_is_shipped_or_named_as_not_shipped():
    """A data file added to the package must be declared or excluded on purpose."""
    declared = dist_check.declared_resources()
    found = {
        path.relative_to(REPO_ROOT / "src").as_posix()
        for path in PACKAGE_DIR.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".py"
    }
    undeclared = sorted(found - declared - NOT_SHIPPED.keys())
    assert not undeclared, (
        f"{undeclared} would not be in the wheel: declare them in "
        "[tool.setuptools.package-data] of pyproject.toml, or add them to NOT_SHIPPED here"
    )
    assert NOT_SHIPPED.keys() <= found, "NOT_SHIPPED names a file that no longer exists"
    assert not NOT_SHIPPED.keys() & declared, "a NOT_SHIPPED file is declared package data"
    assert declared <= found


def test_every_prompt_template_is_registered_and_declared():
    """What `prompts_dir()` holds is exactly TEMPLATE_FILES, and all of it ships."""
    on_disk = sorted(p.name for p in prompts.prompts_dir().glob("*.md"))
    assert on_disk == sorted(prompts.TEMPLATE_FILES), (
        "a template in src/autoforge/prompts/ is not in TEMPLATE_FILES, or the reverse"
    )
    declared = dist_check.declared_resources()
    missing = sorted(
        name for name in prompts.TEMPLATE_FILES if f"autoforge/prompts/{name}" not in declared
    )
    assert not missing, f"templates the wheel would not carry: {missing}"
    assert {f"autoforge/prompts/{n}" for n in prompts.TEMPLATE_FILES} <= (
        dist_check.wheel_package_files()
    )


def _module_reads(path: Path) -> tuple[bool, list[str]]:
    """Whether a module uses ``__file__``, and which resource modules it imports."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    uses_file = any(isinstance(node, ast.Name) and node.id == "__file__" for node in ast.walk(tree))
    imported: list[str] = []
    for node in ast.walk(tree):
        names = (
            [alias.name for alias in node.names]
            if isinstance(node, ast.Import)
            else [node.module or ""]
            if isinstance(node, ast.ImportFrom)
            else []
        )
        imported += [n for n in names if n.startswith(RESOURCE_MODULES)]
    return uses_file, imported


def test_no_module_reads_a_package_file_this_test_does_not_know():
    """A new runtime read relative to the package must be packaged and listed here."""
    reading: dict[str, list[str]] = {}
    for path in sorted(PACKAGE_DIR.rglob("*.py")):
        uses_file, imported = _module_reads(path)
        relative = path.relative_to(REPO_ROOT / "src").as_posix()
        if uses_file:
            reading.setdefault(relative, []).append("__file__")
        if imported:
            reading.setdefault(relative, []).extend(imported)
    unknown = {k: v for k, v in reading.items() if k not in PACKAGE_RELATIVE_READS}
    assert not unknown, (
        f"{unknown} read files relative to the package: declare what they read in "
        "[tool.setuptools.package-data] and list the module in PACKAGE_RELATIVE_READS"
    )
    assert all(v == ["__file__"] for v in reading.values()), reading
    assert sorted(reading) == sorted(PACKAGE_RELATIVE_READS), (
        "PACKAGE_RELATIVE_READS names a module that no longer reads a package file"
    )


def test_the_claimed_interpreters_are_the_ci_matrix():
    """D3: the classifiers claim exactly the CPython versions CI tests, on Linux only."""
    pythons = _ci_pythons()
    versions = [
        c.rsplit(" :: ", 1)[1]
        for c in PROJECT["classifiers"]
        if re.fullmatch(r"Programming Language :: Python :: 3\.\d+", c)
    ]
    assert versions == pythons, f"classifiers claim {versions}, CI tests {pythons}"
    systems = [c for c in PROJECT["classifiers"] if c.startswith("Operating System ::")]
    assert systems == ["Operating System :: POSIX :: Linux"]
    implementations = [c for c in PROJECT["classifiers"] if ":: Implementation ::" in c]
    assert implementations == ["Programming Language :: Python :: Implementation :: CPython"]
    # The floor only: no upper bound, which would break `uv sync` on the
    # development pin and claims nothing (docs/compatibility.md, "Python").
    assert PROJECT["requires-python"] == f">={pythons[0]}"


def test_the_sdist_leaves_out_the_test_suite():
    """setuptools adds tests/test_*.py without their fixtures; MANIFEST.in prunes them."""
    lines = (REPO_ROOT / "MANIFEST.in").read_text(encoding="utf-8").splitlines()
    assert [line for line in lines if line and not line.startswith("#")] == ["prune tests"]
    assert "MANIFEST.in" in dist_check.SDIST_TOP_FILES


# -- the dist check's own checks, on synthetic artifacts ---------------------------
STEM = dist_check.distribution_stem(PROJECT["name"], PROJECT["version"])


def _metadata(**overrides: str) -> str:
    fields: list[tuple[str, str]] = [
        ("Metadata-Version", "2.4"),
        ("Name", PROJECT["name"]),
        ("Version", PROJECT["version"]),
        ("Summary", PROJECT["description"]),
        ("License-Expression", PROJECT["license"]),
        ("Requires-Python", PROJECT["requires-python"]),
        *(("Classifier", c) for c in PROJECT["classifiers"]),
        ("License-File", "LICENSE"),
        ("Provides-Extra", "yaml"),
        ("Requires-Dist", 'pyyaml>=6; extra == "yaml"'),
    ]
    fields = [(k, overrides.get(k, v)) for k, v in fields]
    return "".join(f"{k}: {v}\n" for k, v in fields) + "\nREADME\n"


def _wheel(tmp_path: Path, drop: str = "", metadata: str | None = None) -> Path:
    path = tmp_path / f"{STEM}-py3-none-any.whl"
    info = f"{STEM}.dist-info/"
    with zipfile.ZipFile(path, "w") as archive:
        for name in sorted(dist_check.wheel_package_files() - {drop}):
            archive.writestr(name, (REPO_ROOT / "src" / name).read_bytes())
        archive.writestr(info + "METADATA", metadata or _metadata())
        archive.writestr(
            info + "WHEEL",
            "Wheel-Version: 1.0\nGenerator: synthetic\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
        )
        archive.writestr(
            info + "entry_points.txt", "[console_scripts]\nautoforge = autoforge.cli:main\n"
        )
        archive.writestr(info + "top_level.txt", "autoforge\n")
        archive.writestr(info + "licenses/LICENSE", (REPO_ROOT / "LICENSE").read_bytes())
        archive.writestr(info + "RECORD", "")
    return path


def _sdist(tmp_path: Path, drop: str = "", extra: str = "") -> Path:
    path = tmp_path / f"{STEM}.tar.gz"
    files = {
        name: (REPO_ROOT / name).read_bytes()
        for name in dist_check.SDIST_TOP_FILES - {"PKG-INFO", "setup.cfg"}
    }
    files["PKG-INFO"] = _metadata().encode()
    files["setup.cfg"] = b"[egg_info]\n"
    files[f"src/{STEM.split('-')[0]}.egg-info/SOURCES.txt"] = b""
    for name in dist_check.wheel_package_files() - {drop}:
        files[f"src/{name}"] = (REPO_ROOT / "src" / name).read_bytes()
    if extra:
        files[extra] = b""
    with tarfile.open(path, "w:gz") as archive:
        for name, content in sorted(files.items()):
            member = tarfile.TarInfo(f"{STEM}/{name}")
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return path


def test_the_dist_check_accepts_a_complete_wheel_and_sdist(tmp_path: Path):
    assert dist_check.check_wheel(_wheel(tmp_path), STEM, DATA) == "synthetic"
    dist_check.check_sdist(_sdist(tmp_path), STEM, DATA)


def test_the_dist_check_fails_a_wheel_without_a_template(tmp_path: Path):
    with pytest.raises(CheckFailed, match=r"missing \['autoforge/prompts/review.md'\]"):
        dist_check.check_wheel(_wheel(tmp_path, drop="autoforge/prompts/review.md"), STEM, DATA)


def test_the_dist_check_fails_an_sdist_without_a_template_or_with_a_stray_file(tmp_path: Path):
    with pytest.raises(CheckFailed, match=r"missing \['src/autoforge/prompts/fix.md'\]"):
        dist_check.check_sdist(_sdist(tmp_path, drop="autoforge/prompts/fix.md"), STEM, DATA)
    with pytest.raises(CheckFailed, match=r"should not: \['tests/test_cli.py'\]"):
        dist_check.check_sdist(_sdist(tmp_path, extra="tests/test_cli.py"), STEM, DATA)


@pytest.mark.parametrize(
    "field, value",
    [
        ("Name", "autoforge"),
        ("Version", "9.9.9"),
        ("License-Expression", "Apache-2.0"),
        ("Metadata-Version", "2.3"),
        ("Requires-Python", ">=3.10"),
        ("Requires-Dist", "pyyaml>=6"),
    ],
)
def test_the_dist_check_fails_metadata_that_is_not_pyproject_tomls(
    tmp_path: Path, field: str, value: str
):
    wheel = _wheel(tmp_path, metadata=_metadata(**{field: value}))
    with pytest.raises(CheckFailed, match=field if field != "Requires-Dist" else "dependency"):
        dist_check.check_wheel(wheel, STEM, DATA)


@pytest.mark.parametrize(
    "tag, version",
    [
        ("v0.1.0a0", "0.1.0a0"),
        ("v0.1.0a1", "0.1.0a1"),
        ("v0.1.0rc1", "0.1.0rc1"),
        ("v1.2.3", "1.2.3"),
    ],
)
def test_a_release_tag_names_its_version(tag: str, version: str):
    dist_check.check_release_tag(tag, version)


@pytest.mark.parametrize(
    "tag, version, reason",
    [
        ("0.1.0a0", "0.1.0a0", "does not name"),
        ("v0.1.0", "0.1.0a0", "does not name"),
        ("v0.1.0a0 ", "0.1.0a0", "does not name"),
        ("refs/tags/v0.1.0a0", "0.1.0a0", "does not name"),
        ("v0.1.0-alpha0", "0.1.0a0", "does not name"),
        ("v0.1.0.dev1", "0.1.0.dev1", "not a release version"),
        ("v0.1.0a0+local", "0.1.0a0+local", "not a release version"),
        ("v01.1.0", "01.1.0", "not a release version"),
    ],
)
def test_the_release_build_refuses_a_tag_that_is_not_its_version(
    tag: str, version: str, reason: str
):
    with pytest.raises(CheckFailed, match=reason):
        dist_check.check_release_tag(tag, version)


def test_the_release_build_refuses_a_wrong_tag_before_it_builds(monkeypatch, capsys):
    """`--release-tag` is checked first: a wrong tag builds, installs and uploads nothing."""

    def no_build(*args, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("built for a tag that does not name the version")

    monkeypatch.setattr(dist_check, "copy_source", no_build)
    monkeypatch.setattr(dist_check, "build", no_build)
    assert dist_check.main(["--release-tag", "v0.0.0"]) == 1
    assert "does not name the package's version" in capsys.readouterr().err


def test_the_digests_become_the_steps_outputs_and_sha256sums(tmp_path: Path, monkeypatch):
    """What pypi-publish.yml's publish job re-verifies is what the build job wrote."""
    sdist = tmp_path / f"{STEM}.tar.gz"
    wheel = tmp_path / f"{STEM}-py3-none-any.whl"
    sdist.write_bytes(b"sdist")
    wheel.write_bytes(b"wheel")
    output, summary, sums = tmp_path / "output", tmp_path / "summary", tmp_path / "SHA256SUMS"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(summary))
    dist_check.record_digests([sdist, wheel], sums, PROJECT["version"])
    lines = [
        f"{hashlib.sha256(b'sdist').hexdigest()}  {sdist.name}",
        f"{hashlib.sha256(b'wheel').hexdigest()}  {wheel.name}",
    ]
    assert sums.read_text(encoding="utf-8") == "".join(f"{line}\n" for line in lines)
    assert output.read_text(encoding="utf-8") == (
        f"version={PROJECT['version']}\n"
        "sha256sums<<SHA256SUMS_END\n" + "".join(f"{line}\n" for line in lines) + "SHA256SUMS_END\n"
    )
    assert all(line in summary.read_text(encoding="utf-8") for line in lines)


# What the installed command prints on a REMOTE dry run, as far as the dist check
# looks; the stand-ins below print it and then misbehave in one way each.
_REMOTE_PLAN = (
    'echo "Template: analyze_execute.md"; echo "--- prompt preview ---"; '
    'echo "<<<CONTROL_RESULT>>>"'
)


@pytest.mark.parametrize(
    "then, refusal",
    [
        ("", None),
        ("gh api user || true", r"called gh: 'api user\\n'"),
        ("mkdir .autoforge", r"changed the scratch repository: \['\.autoforge'\]"),
        (": > .git/index.lock", r"changed the scratch repository: \['\.git/index\.lock'\]"),
        ("echo x >> .git/HEAD", r"changed the scratch repository: \['\.git/HEAD'\]"),
        ('echo "{{ISSUE_URL}}"', "unrendered variable"),
    ],
)
def test_the_remote_dry_run_check_refuses_a_gh_call_and_any_write(
    tmp_path: Path, then: str, refusal: str | None
):
    """``remote_dry_run`` on a stand-in for the installed command (real git, fake gh)."""
    command = tmp_path / "autoforge"
    command.write_text(f"#!/bin/sh\n{_REMOTE_PLAN}\n{then}\n", encoding="utf-8")
    command.chmod(0o755)
    git = dist_check.shutil.which("git")
    assert git is not None
    if refusal is None:
        dist_check.remote_dry_run(str(command), git, tmp_path, dist_check._child_env())
        return
    with pytest.raises(CheckFailed, match=refusal):
        dist_check.remote_dry_run(str(command), git, tmp_path, dist_check._child_env())
