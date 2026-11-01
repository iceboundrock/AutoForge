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
# The versions a release may have, and those `main` may carry between releases
# (docs/releasing.md), shared with the release build's tag check.
RELEASE_VERSION = dist_check.RELEASE_VERSION
DEVELOPMENT_VERSION = dist_check.DEVELOPMENT_VERSION
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


def test_the_build_backend_is_pinned_with_its_hashes():
    """build-constraints.txt pins exactly the build requirements, each with a SHA-256 (#295)."""
    pins = dist_check.build_constraints()
    requires = {}
    for requirement in DATA["build-system"]["requires"]:
        match = re.fullmatch(r"([A-Za-z0-9._-]+)>=(\d+(?:\.\d+)*)", requirement)
        assert match, f"unexpected build requirement {requirement!r}; update this test"
        requires[match.group(1).lower()] = tuple(int(n) for n in match.group(2).split("."))
    assert set(pins) == set(requires)
    for name, (version, hashes) in pins.items():
        assert re.fullmatch(r"\d+(\.\d+)*", version), f"{name}=={version} is not a final release"
        assert tuple(int(n) for n in version.split(".")) >= requires[name]
        assert len(set(hashes)) == len(hashes) >= 1


_HASH_0, _HASH_1 = (f"--hash=sha256:{digit * 64}" for digit in "01")


@pytest.mark.parametrize(
    "text, reason",
    [
        ("setuptools>=84\n", "is not name==version"),
        ("setuptools==84.2.1\n", "needs --hash=sha256"),
        ("setuptools==84.2.1 --hash=md5:00\n", "needs --hash=sha256"),
        (f"setuptools==84.2.1 --index-url https://example.invalid {_HASH_0}", "needs --hash"),
        (f"setuptools==84.2.1 {_HASH_0}\nSetuptools==84.2.0 {_HASH_1}\n", "pinned twice"),
        ("# nothing\n", "pins nothing"),
    ],
)
def test_the_build_constraints_refuse_a_pin_without_a_hash(tmp_path: Path, text: str, reason: str):
    (tmp_path / dist_check.BUILD_CONSTRAINTS).write_text(text, encoding="utf-8")
    with pytest.raises(CheckFailed, match=reason):
        dist_check.build_constraints(tmp_path)


def test_the_build_constraints_read_continued_lines(tmp_path: Path):
    digest = "a" * 64
    (tmp_path / dist_check.BUILD_CONSTRAINTS).write_text(
        f"# comment\nsetuptools==84.2.1 \\\n    --hash=sha256:{digest}  # wheel\n", encoding="utf-8"
    )
    assert dist_check.build_constraints(tmp_path) == {
        "setuptools": ("84.2.1", (f"--hash=sha256:{digest}",))
    }


def test_the_version_has_one_value():
    """pyproject.toml and `autoforge.__version__` agree, in the version scheme's form."""
    assert PROJECT["version"] == autoforge.__version__
    assert DEVELOPMENT_VERSION.fullmatch(PROJECT["version"]), (
        f"{PROJECT['version']!r} is not X.Y.Z[aN|bN|rcN][.devM] (docs/releasing.md)"
    )


@pytest.mark.parametrize(
    "version, released",
    [
        ("0.1.0a0", True),
        ("0.1.0a1", True),
        ("0.1.0rc1", True),
        ("1.2.3", True),
        ("0.1.0a1.dev0", False),
        ("0.1.0a1.dev12", False),
        ("0.1.0.dev0", False),
    ],
)
def test_main_carries_a_release_or_a_development_version(version: str, released: bool):
    """`main` may carry X.Y.Z[aN|bN|rcN].devM between releases; only the rest is released."""
    assert DEVELOPMENT_VERSION.fullmatch(version)
    assert bool(RELEASE_VERSION.fullmatch(version)) is released


@pytest.mark.parametrize(
    "version",
    ["0.1.0a1.dev", "0.1.0a1.dev01", "0.1.0a1-dev0", "0.1.0a1.post1", "0.1.0a1.dev0+local", "0.1"],
)
def test_main_carries_no_other_version(version: str):
    assert not DEVELOPMENT_VERSION.fullmatch(version)


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


def _wheel(tmp_path: Path, drop: str = "", metadata: str | None = None, stem: str = STEM) -> Path:
    path = tmp_path / f"{stem}-py3-none-any.whl"
    info = f"{stem}.dist-info/"
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


@pytest.mark.parametrize(
    "wheel_file, sdist_file",
    [
        ("autoforge/prompts/local_common.md", "src/autoforge/prompts/local_review.md"),
        ("autoforge/run_contract.py", "src/autoforge/local_workspace.py"),
    ],
    ids=["template", "module"],
)
def test_no_artifact_carries_a_file_of_local_mode(tmp_path: Path, wheel_file, sdist_file):
    """ADR 0009 D12 S3 (#388), checked again by S4 (#389): the wheel and the
    sdist carry none of LOCAL mode's files, its ``local_*.md`` prompt
    templates or its two modules, ``local_workspace.py`` and
    ``run_contract.py``.

    What the package declares holds none, so the wheel built from this
    commit, which ``check_wheel`` holds to exactly that set, holds none; and
    the dist check refuses a wheel or an sdist that carries one anyway, as
    a stray file would be refused, but naming LOCAL.
    """
    assert dist_check.local_files(dist_check.wheel_package_files()) == []
    assert not list(prompts.prompts_dir().glob("local_*.md"))
    assert [name for name in prompts.TEMPLATE_FILES if name.startswith("local_")] == []
    assert not [
        m
        for m in dist_check.LOCAL_MODULES
        if (REPO_ROOT / "src").joinpath(*m.split(".")).with_suffix(".py").exists()
    ]
    wheel = _wheel(tmp_path)
    with zipfile.ZipFile(wheel) as archive:
        assert dist_check.local_files(archive.namelist()) == []
    with zipfile.ZipFile(wheel, "a") as archive:
        archive.writestr(wheel_file, "# LOCAL\n")
    with pytest.raises(CheckFailed, match=rf"LOCAL mode's files: \['{re.escape(wheel_file)}'\]"):
        dist_check.check_wheel(wheel, STEM, DATA)
    sdist = _sdist(tmp_path, extra=sdist_file)
    with pytest.raises(CheckFailed, match=rf"LOCAL mode's files: \['{re.escape(sdist_file)}'\]"):
        dist_check.check_sdist(sdist, STEM, DATA)


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
        ("v0.1.0a1.dev0", "0.1.0a1.dev0", "never released"),
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
    """`--release-tag` is checked first: a wrong tag builds, installs and uploads nothing.

    The version is a release one whatever `main` carries, so the tag is what is
    refused; a `.devM` version is refused on its own (the test below).
    """
    release = {**DATA, "project": {**PROJECT, "version": "0.1.0a1"}}

    def no_build(*args, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("built for a tag that does not name the version")

    monkeypatch.setattr(dist_check, "pyproject", lambda root=REPO_ROOT: release)
    monkeypatch.setattr(dist_check, "copy_source", no_build)
    monkeypatch.setattr(dist_check, "build", no_build)
    assert dist_check.main(["--release-tag", "v0.0.0"]) == 1
    assert "does not name the package's version" in capsys.readouterr().err


def test_a_development_version_builds_but_is_never_released(tmp_path: Path, monkeypatch, capsys):
    """A .devM `main` passes the artifact checks; `--release-tag` refuses it before it builds."""
    version = "0.1.0a1.dev0"
    data = {**DATA, "project": {**PROJECT, "version": version}}
    stem = dist_check.distribution_stem(PROJECT["name"], version)
    assert stem == "autoforge_cli-0.1.0a1.dev0"
    wheel = _wheel(tmp_path, metadata=_metadata(Version=version), stem=stem)
    assert dist_check.check_wheel(wheel, stem, data) == "synthetic"

    def no_build(*args, **kwargs):  # pragma: no cover - reaching it is the failure
        raise AssertionError("built a release of a development version")

    monkeypatch.setattr(dist_check, "pyproject", lambda root=REPO_ROOT: data)
    monkeypatch.setattr(dist_check, "copy_source", no_build)
    monkeypatch.setattr(dist_check, "build", no_build)
    assert dist_check.main(["--release-tag", f"v{version}"]) == 1
    assert "a .devM version is never released" in capsys.readouterr().err


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
        ("mkdir .autoforge", r"changed the scratch repository: \[.*'\.autoforge \(created\)'\]"),
        (": > .git/index.lock", r"repository: \[.*'\.git/index\.lock \(created\)'\]"),
        ("echo x >> .git/HEAD", r"\['\.git/HEAD \(content, size, mtime, ctime\)'\]"),
        # The same bytes written over themselves, in place (PR #425 R1-F4).
        ("head -c 1 .git/HEAD 1<> .git/HEAD", r"\['\.git/HEAD \(mtime, ctime\)'\]"),
        ('echo "{{ISSUE_URL}}"', "unrendered variable"),
    ],
    ids=[
        "clean",
        "gh-called",
        "dir-created",
        "lock-created",
        "appended",
        "same-bytes",
        "unrendered",
    ],
)
def test_the_remote_dry_run_check_refuses_a_gh_call_and_any_write(
    tmp_path: Path, then: str, refusal: str | None
):
    """``remote_dry_run`` on a stand-in for the installed command (real git, fake gh):
    any write shows, the bytes a file already held written over it again included."""
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


# What the installed command answers LOCAL mode's ways in with, as far as the
# dist check looks (ADR 0009 D3 to D5); the stand-ins below answer so and then
# misbehave in one way each, before the answer.
_LOCAL_ANSWERS = """
case "$*" in
  --help) echo "usage: autoforge {doctor,run,step,resume,unblock,status,epic}";;
  local*) echo "autoforge: error: argument command: invalid choice: 'local'" >&2; exit 2;;
  --config*) echo "autoforge: error: c: unknown key(s) under 'the top level': local" >&2; exit 2;;
  *) echo "autoforge: error: state file has unknown mode 'LOCAL'" >&2; exit 2;;
esac
"""


# How a misbehaving stand-in tells an ordinary invocation from its dry run:
# `step`, `resume` and `unblock --reason looked` without `--dry-run`.
_ORDINARY = {
    "step": '[ "$1" = step ] && [ $# -eq 1 ]',
    "resume": '[ "$1" = resume ] && [ $# -eq 1 ]',
    "unblock": '[ "$1" = unblock ] && [ $# -eq 3 ]',
    "run": '[ "$1" = run ]',
}
_STATE = ".autoforge/state.json"


@pytest.mark.parametrize(
    "first, refusal",
    [
        ("", None),
        (
            '[ "$1" = --help ] && echo "usage: autoforge {local,run}" && exit 0',
            "`autoforge --help` names `local`",
        ),
        ('[ "$1" = local ] && [ "$2" = doctor ] && exit 0', r"local doctor` exited 0, not 2"),
        ('[ "$2" = --json ] && echo "{}" && exit 0', r"status --json` exited 0, not 2"),
        (
            '[ "$1" = --config ] && echo "autoforge: error: unreadable" >&2 && exit 2',
            r"exited 2, not 2 naming \"unknown key\(s\) under 'the top level': local\"",
        ),
        (
            f'[ "$1" = step ] && mv {_STATE} {_STATE}.corrupt-1',
            r"step --dry-run` changed the scratch repository: \['\.autoforge \(.*?\)', "
            r"'\.autoforge/state\.json \(removed\)', '\.autoforge/state\.json\.corrupt-1 "
            r"\(created\)'\]",
        ),
        (
            '[ "$1" = unblock ] && echo x >> .autoforge/logs/local-run/run.log',
            r"changed the scratch repository: \['\.autoforge/logs/local-run/run\.log "
            r"\(content, size, mtime, ctime\)'\]",
        ),
        ('[ "$1" = resume ] && : > .git/index.lock', r"'\.git/index\.lock \(created\)'\]"),
        ('[ "$1" = status ] && { gh api user || true; }', r"status` called 'gh api user\\n'"),
        (
            '[ "$1" = --config ] && echo "# edited" >> "$2"',
            r"status` changed the configuration file .*local-config\.json: "
            r"\['local-config\.json \(content, size, mtime, ctime\)'\]",
        ),
        # PR #425 R1-F3: what only an ordinary invocation does, each of its
        # dry run refused as it should be. It takes the controller lock, or
        # launches an agent, before it refuses.
        (
            f"{_ORDINARY['step']} && mkdir -p .git/autoforge && echo $$ > "
            ".git/autoforge/controller.lock",
            r"`\S*autoforge step` changed the scratch repository: .*"
            r"'\.git/autoforge/controller\.lock \(created\)'\]",
        ),
        (
            f"{_ORDINARY['step']} && : > .git/autoforge.lock && rm .git/autoforge.lock",
            r"`\S*autoforge step` changed the scratch repository: \['\.git \(.*?mtime, ctime\)'\]",
        ),
        (
            f"{_ORDINARY['resume']} && {{ claude -p 'review the PR' || true; }}",
            r"`\S*autoforge resume` called 'claude -p review the PR\\n'",
        ),
        (
            f"{_ORDINARY['unblock']} && {{ opencode run --standalone x || true; }}",
            r"`\S*autoforge unblock --reason looked` called 'opencode run --standalone x\\n'",
        ),
        (
            f"{_ORDINARY['run']} && {{ pi --mode rpc || true; }}",
            r"autoforge run --epic \S+ --issue \S+` called 'pi --mode rpc\\n'",
        ),
        # PR #425 R1-F4: writes that leave every path and every byte as it was.
        (
            f"{_ORDINARY['unblock']} && head -c 1 {_STATE} 1<> {_STATE}",
            r"unblock --reason looked` changed the scratch repository: "
            r"\['\.autoforge/state\.json \(mtime, ctime\)'\]",
        ),
        (
            f"{_ORDINARY['run']} && cp -p {_STATE} {_STATE}.new && mv {_STATE}.new {_STATE}",
            r"changed the scratch repository: \['\.autoforge \(.*?\)', "
            r"'\.autoforge/state\.json \(inode, ctime\)'\]",
        ),
        (
            f'[ "$1" = step ] && [ "$2" = --dry-run ] && touch -r {_STATE} {_STATE}',
            r"step --dry-run` changed the scratch repository: "
            r"\['\.autoforge/state\.json \(ctime\)'\]",
        ),
        (
            '[ "$1" = --config ] && touch -r "$2" "$2"',
            r"status` changed the configuration file .*: \['local-config\.json \(ctime\)'\]",
        ),
        (
            f'{_ORDINARY["resume"]} && : > "$HOME/.config/autoforge-was-here"',
            r"resume` changed the scratch HOME: \['\.config \(.*?\)', "
            r"'\.config/autoforge-was-here \(created\)'\]",
        ),
        (
            f'{_ORDINARY["step"]} && : > "$TMPDIR/x" && rm "$TMPDIR/x"',
            r"step` changed the scratch TMPDIR: \['\. \(.*?mtime, ctime\)'\]",
        ),
    ],
    ids=[
        "refused",
        "help-names-local",
        "local-accepted",
        "status-json-accepted",
        "config-wrong-refusal",
        "state-moved-aside",
        "log-written",
        "git-dir-written",
        "gh-called",
        "config-written",
        "step-takes-the-lock",
        "step-takes-and-drops-a-lock",
        "resume-launches-claude",
        "unblock-launches-opencode",
        "run-launches-pi",
        "state-rewritten-same-bytes",
        "state-replaced-by-a-copy",
        "state-times-put-back",
        "config-times-put-back",
        "home-written",
        "tmpdir-used",
    ],
)
def test_the_local_refusal_check_holds_every_way_in_to_its_refusal(
    tmp_path: Path, first: str, refusal: str | None
):
    """``local_refusals`` (#389) on a stand-in for the installed command
    (real git, recording fakes of gh and the agent CLIs): it passes a
    command that answers each of LOCAL mode's ways in with its generic
    refusal and touches nothing, and fails one that accepts a way in,
    refuses it for another reason, writes to the state directory, its log
    or ``.git``, edits the configuration file or calls ``gh``. Also one
    whose ordinary ``step``, ``resume``, ``unblock`` or ``run`` takes a
    lock or launches an agent CLI before it refuses, while its dry run
    behaves (PR #425 R1-F3), and one that writes without changing a byte
    or a path: a file rewritten in place, replaced by a copy of itself, or
    with its times put back, a file created and removed in the scratch
    ``HOME`` or ``TMPDIR`` (R1-F4)."""
    command = tmp_path / "autoforge"
    command.write_text(f"#!/bin/sh\n{first}\n{_LOCAL_ANSWERS}", encoding="utf-8")
    command.chmod(0o755)
    git = dist_check.shutil.which("git")
    assert git is not None
    if refusal is None:
        dist_check.local_refusals(str(command), git, tmp_path, dist_check._child_env())
        return
    with pytest.raises(CheckFailed, match=refusal):
        dist_check.local_refusals(str(command), git, tmp_path, dist_check._child_env())


def test_the_local_refusal_check_runs_in_an_environment_of_its_own(tmp_path: Path, monkeypatch):
    """PR #425 R1-F3: what reaches the installed command is the isolated
    environment alone. No credential or provider key of the caller's, its
    ``HOME`` and ``TMPDIR`` scratch directories, and every executable a
    regression could reach a service or a model through a recording fake
    first on ``PATH``."""
    monkeypatch.setenv("GH_TOKEN", "not-a-real-token-but-must-not-pass")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key-but-must-not-pass")
    seen = tmp_path / "seen.env"
    command = tmp_path / "autoforge"
    command.write_text(
        f'#!/bin/sh\n[ "$1" = local ] && [ $# -eq 1 ] && env > {seen}\n{_LOCAL_ANSWERS}',
        encoding="utf-8",
    )
    command.chmod(0o755)
    git = dist_check.shutil.which("git")
    assert git is not None
    dist_check.local_refusals(str(command), git, tmp_path, dist_check._child_env())
    names = {line.split("=", 1)[0] for line in seen.read_text().splitlines() if "=" in line}
    assert "GH_TOKEN" not in names and "ANTHROPIC_API_KEY" not in names
    env = dict(line.split("=", 1) for line in seen.read_text().splitlines() if "=" in line)
    assert env["HOME"] == str(tmp_path / "local-home")
    assert env["TMPDIR"] == str(tmp_path / "local-tmp")
    fake_bin = env["PATH"].split(":")[0]
    assert fake_bin == str(tmp_path / "fake-bin-local")
    assert sorted(p.name for p in Path(fake_bin).iterdir()) == sorted(
        dist_check._RECORDED_EXECUTABLES
    )
    assert {"gh", "claude", "opencode", "pi"} <= set(dist_check._RECORDED_EXECUTABLES)


def test_the_local_refusal_check_covers_each_way_in_adr_0009_names():
    """ADR 0009 D3 to D5: the refusal check covers ``autoforge local``, a
    ``local:`` key and a ``mode: "LOCAL"`` state under each command that
    loads one; ``step``, ``resume`` and ``unblock`` both as dry runs and as
    the ordinary invocations that take the lock and launch, and ``run``
    over the state (PR #425 R1-F3)."""
    ways = {words: (status, refusal) for words, status, refusal in dist_check._LOCAL_WAYS_IN}
    assert {w for w in ways if w[0] == "local"} >= {("local",), ("local", "doctor")}
    assert ways[("--config", "{config}", "status")][1].endswith("'the top level': local")
    loading = {w for w, (_, refusal) in ways.items() if "unknown mode 'LOCAL'" in refusal}
    assert {w[0] if w[0] != "--state-dir" else w[2] for w in loading} == {
        "status",
        "step",
        "resume",
        "unblock",
        "run",
    }
    # Each command that could take the lock and launch, as a dry run and as
    # the ordinary invocation that would (PR #425 R1-F3), and `run` over it.
    for command in ("step", "resume", "unblock"):
        forms = {"--dry-run" in w for w in loading if w[0] == command}
        assert forms == {True, False}, command
    assert any(w[0] == "run" and "--dry-run" not in w for w in loading)
    assert {status for status, _ in ways.values()} == {2}


def test_the_scratch_repository_starts_no_detached_maintenance(tmp_path: Path):
    """``git commit`` leaves ``git maintenance run --auto --detach`` running
    (Git 2.47 and later), which takes ``.git/objects/maintenance.lock`` after
    the commit has returned. The REMOTE dry run compares the scratch
    repository before and after it, and under pytest-xdist's load saw that
    write as the dry run's (#340); so its commit runs with automatic
    maintenance off. GIT_TRACE records every command git starts."""
    git = dist_check.shutil.which("git")
    assert git is not None
    trace = tmp_path / "trace"
    env = dict(dist_check._child_env(), GIT_TRACE=str(trace))
    dist_check._scratch_repository(git, tmp_path / "repo", env)
    text = trace.read_text(encoding="utf-8")
    assert "trace: built-in: git commit" in text
    assert "maintenance" not in text
