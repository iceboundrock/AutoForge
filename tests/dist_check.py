"""Build AutoForge's distributions and prove that the installed wheel works (#256).

``make dist-check`` runs this, and so does the ``dist`` job of
``.github/workflows/ci.yml`` on every Python the CI matrix tests. It needs
``uv`` and ``git`` on ``PATH`` and nothing else: no GitHub call, no
credential, no agent CLI. Its only network access is ``uv`` fetching the
build backend (``[build-system] requires``) from the package index, pinned
by ``build-constraints.txt`` and installed only if its SHA-256 is one that
file lists (``--build-constraints``, ``--require-hashes``).

It runs on the interpreter it checks (``uv run --no-project --python X
python -I tests/dist_check.py``). Isolated mode (``-I``) keeps this file's
directory and the working directory off ``sys.path``, so nothing here ever
imports the ``autoforge`` of the source tree:

1. The files git sees in the source tree (tracked, and untracked unless
   ignored) are copied to a scratch directory outside it, so that nothing a
   development tree accumulates (a stale ``*.egg-info``, whose
   ``SOURCES.txt`` setuptools would carry into the sdist, or ``build/``)
   reaches the artifacts, and the tree itself is never written. ``uv build``
   writes the sdist of that copy, then a wheel built from that sdist, into
   ``--out-dir`` (else a temporary directory outside the source tree). The
   wheel must name the pinned backend as its ``Generator``.
2. The sdist holds exactly ``LICENSE``, ``MANIFEST.in``, ``README.md``,
   ``pyproject.toml``, ``PKG-INFO``, ``setup.cfg``, the egg-info, the modules and the
   resources ``[tool.setuptools.package-data]`` declares. The wheel holds
   exactly those modules and resources and its dist-info. Neither carries a
   file of LOCAL mode's, which its removal deleted (ADR 0009 D12 S3, #388),
   whatever the tree declares: a prompt template
   ``autoforge/prompts/local_*.md``, ``autoforge/local_workspace.py`` or
   ``autoforge/run_contract.py``. Both carry the
   metadata ``pyproject.toml`` declares (``Name: autoforge-cli``, the
   version, ``License-Expression: MIT``, ``License-File: LICENSE``, the
   classifiers, ``Requires-Python``) and ``LICENSE`` byte for byte. A file
   the sdist dropped is therefore missing from the wheel, and fails here.
3. A fresh virtual environment outside the source tree gets the wheel,
   installed non-editably from the file alone (``uv pip install
   --no-index``).
4. From that environment: ``autoforge --version`` and ``autoforge --help``;
   every prompt template loaded and rendered from the installed package
   (this file again, with ``--installed``, on the environment's own
   interpreter); and a REMOTE ``autoforge run --dry-run --full-prompt`` in
   a scratch git repository, with a ``gh`` on ``PATH`` that records any
   call, answers the EPIC binding's two issue reads (#293) and fails every
   other call, which must print the binding and render the ANALYZE_EXECUTE
   prompt through the installed engine, call ``gh`` for those two reads
   alone and leave the repository unchanged (:func:`_tree`: every path's
   bytes, identity and metadata). Then LOCAL
   mode's ways in, each met by a generic refusal of the installed command
   (ADR 0009 D3 to D5, #389): ``--help`` names no ``local``; ``autoforge
   local`` is argparse's invalid choice; a configuration with a ``local:``
   block is an unknown key; a ``mode: "LOCAL"`` state is an unknown mode to
   every command that loads it, ``step``, ``resume`` and ``unblock`` both
   as dry runs and as the ordinary invocations that would take the lock and
   launch, and ``run`` over it. Each exits 2, in an environment of its own
   (a scratch ``HOME`` and ``TMPDIR``, and recording fakes of ``gh``,
   ``claude``, ``opencode`` and ``pi`` first on ``PATH``, so that a
   regression reaches no service and no model), runs none of the fakes,
   and leaves unchanged the repository, its state directory and ``.git``
   included, the configuration file, the scratch ``HOME`` and ``TMPDIR``.
5. The offline half of a stranger's first run (#259, docs/getting-started.md):
   ``uv tool install`` of the wheel alone, into tool directories and a home
   of their own; then, with no credential and no agent CLI on ``PATH``,
   ``autoforge --version`` and ``--help``, and ``autoforge doctor --json``
   in a repository whose ``origin`` is on GitHub, with the built-in
   routing and with each starter configuration of ``examples/``: every row
   that needs no credential is OK, the merge gate is closed, and the agent
   rows name the CLIs the configuration routes to. A REMOTE dry run with
   each starter, and the repository's ``git status`` stays empty.
6. The SHA-256 of each artifact is printed, appended to the job summary
   (``$GITHUB_STEP_SUMMARY``) when that is set, and written to
   ``--sha256sums`` in ``sha256sum -c`` format when asked. When
   ``$GITHUB_OUTPUT`` is set, the version and those lines become the step's
   outputs ``version`` and ``sha256sums``.

``--release-tag TAG`` builds a release (``.github/workflows/pypi-publish.yml``,
docs/releasing.md): before anything is built it refuses a tag other than
``v<version>``, and a version outside the release scheme.

Exit status 0 means every check passed. A failure says what failed, keeps
the scratch directory for inspection and exits 1.
"""

from __future__ import annotations

import argparse
import configparser
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.message import Message
from email.parser import HeaderParser
from pathlib import Path
from typing import NamedTuple

REPO_ROOT = Path(__file__).resolve().parent.parent
PACKAGE = "autoforge"
# The command the wheel installs, and where it points.
CONSOLE_SCRIPT = ("autoforge", "autoforge.cli:main")
# What the sdist carries besides src/ (MANIFEST.in prunes tests/).
SDIST_TOP_FILES = frozenset(
    {"LICENSE", "MANIFEST.in", "PKG-INFO", "README.md", "pyproject.toml", "setup.cfg"}
)
# The oldest core-metadata version with License-Expression (PEP 639).
MIN_METADATA_VERSION = (2, 4)
# The versions a release may have (docs/releasing.md): PEP 440 in its canonical
# form, X.Y.Z with an optional aN / bN / rcN pre-release. A release's tag is `v`
# and that version.
RELEASE_VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)((a|b|rc)(0|[1-9]\d*))?")
# The versions `main` may carry: a release version, or one with a PEP 440
# development suffix .devM between releases (0.1.0a1.dev0 after 0.1.0a0 is
# published). A release build refuses a .devM version.
DEVELOPMENT_VERSION = re.compile(RELEASE_VERSION.pattern + r"(\.dev(0|[1-9]\d*))?")
# The build backend's pins, with their hashes (#295).
BUILD_CONSTRAINTS = "build-constraints.txt"
_PIN = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)==([0-9][0-9A-Za-z.!+-]*)")
_HASH = re.compile(r"--hash=sha256:[0-9a-f]{64}")
# The URLs of the REMOTE dry run. A dry run binds the run's EPIC (#293): it
# reads the issue and the EPIC that binding names through `gh api`, and calls
# `gh` for nothing else. The fake `gh` answers those two reads as GitHub does:
# issue #2 is a native sub-issue of #1, the EPIC --epic names too.
REMOTE_EPIC = "https://github.com/example/dist-check/issues/1"
REMOTE_ISSUE = "https://github.com/example/dist-check/issues/2"
_REMOTE_REPOSITORY = "example/dist-check"


def _issue_row(number: int, parent: int | None) -> str:
    """``GET /repos/<repository>/issues/<number>`` as GitHub answers it, as one JSON line.

    Open, with the native sub-issue summaries an API with sub-issues sends,
    and ``parent_issue_url`` only when the issue has a parent (GitHub leaves
    it out otherwise).
    """
    row: dict[str, object] = {
        "id": 9_000 + number,
        "number": number,
        "html_url": f"https://github.com/{_REMOTE_REPOSITORY}/issues/{number}",
        "repository_url": f"https://api.github.com/repos/{_REMOTE_REPOSITORY}",
        "state": "open",
        "state_reason": None,
        "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
        "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0},
    }
    if parent is not None:
        row["parent_issue_url"] = (
            f"https://api.github.com/repos/{_REMOTE_REPOSITORY}/issues/{parent}"
        )
    return json.dumps(row)


# The `gh` arguments of the binding's reads, in the order a dry run makes them,
# and the answer the fake `gh` gives each.
_BINDING_ANSWERS = {
    f"api repos/{_REMOTE_REPOSITORY}/issues/2": _issue_row(2, parent=1),
    f"api repos/{_REMOTE_REPOSITORY}/issues/1": _issue_row(1, parent=None),
}
_PLACEHOLDER = re.compile(r"\{\{\s*[A-Z][A-Z0-9_]*\s*\}\}")
# LOCAL mode's packaged files, deleted with it (ADR 0009 D12 S3, #388): its four
# prompt templates and its two modules of its own. No artifact and no installed
# package may carry one again.
_LOCAL_FILE = re.compile(
    r"(?:^|/)autoforge/(?:prompts/local_[^/]*\.md|local_workspace\.py|run_contract\.py)$"
)
LOCAL_MODULES = ("autoforge.local_workspace", "autoforge.run_contract")
_CONTROL_RESULT = "<<<CONTROL_RESULT>>>"
# The first run (#259, docs/getting-started.md). Its repository's origin is the
# repository of the REMOTE URLs.
FIRST_RUN_ORIGIN = "https://github.com/example/dist-check.git"
# What the guide keeps out of git (docs/security.md).
FIRST_RUN_EXCLUDES = (".autoforge/", "autoforge.yaml", "autoforge.local.*")
# The starter configurations under examples/, each with the agent CLIs it
# routes a profile to, and the CLIs of the built-in routing (no --config).
STARTER_AGENTS = {
    "starter-claude-only.yaml": frozenset({"claude"}),
    "starter-two-providers.yaml": frozenset({"claude", "opencode"}),
}
BUILT_IN_AGENTS = frozenset({"claude", "opencode"})
# The doctor rows that need no credential, no agent CLI and no network: OK on
# a first run before anything is signed in.
CREDENTIAL_FREE_ROWS = (
    "controller",
    "config",
    "merge gate",
    "git available",
    "cwd is a git repository",
    "GitHub remote",
    "launch environment",
    "no credential reaches a launch",
    "profile limits",
    "loop detection",
    "state dir writable",
    "scheduler settings",
)
_AGENT_ROW = re.compile(r"agent '([^']+)' available \(.*\)")


class CheckFailed(Exception):
    """One check of the artifacts failed; the message says which and why."""


# -- what the source tree declares ----------------------------------------------
def pyproject(root: Path = REPO_ROOT) -> dict:
    return tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))


def package_modules(root: Path = REPO_ROOT) -> set[str]:
    """Every module of the package, as its path in a wheel (``autoforge/cli.py``)."""
    src = root / "src"
    return {
        path.relative_to(src).as_posix()
        for path in (src / PACKAGE).rglob("*.py")
        if "__pycache__" not in path.parts
    }


def declared_resources(root: Path = REPO_ROOT, data: dict | None = None) -> set[str]:
    """Every file ``[tool.setuptools.package-data]`` declares, as its path in a wheel."""
    data = pyproject(root) if data is None else data
    src = root / "src"
    declared: set[str] = set()
    package_data = data.get("tool", {}).get("setuptools", {}).get("package-data", {})
    for package, patterns in package_data.items():
        directory = src.joinpath(*package.split("."))
        for pattern in patterns:
            declared |= {
                path.relative_to(src).as_posix()
                for path in directory.glob(pattern)
                if path.is_file() and "__pycache__" not in path.parts
            }
    return declared


def wheel_package_files(root: Path = REPO_ROOT) -> set[str]:
    """What the wheel must hold outside its dist-info: the modules and the resources."""
    return package_modules(root) | declared_resources(root)


def local_files(names) -> list[str]:
    """LOCAL mode's files among ``names``: a ``prompts/local_*.md``, or one of its modules."""
    return sorted(name for name in names if _LOCAL_FILE.search(name))


def distribution_stem(name: str, version: str) -> str:
    """The file-name stem of a distribution (``autoforge_cli-0.1.0``), PEP 625 / 427."""
    return f"{re.sub(r'[-_.]+', '_', name).lower()}-{version}"


# -- helpers ------------------------------------------------------------------
def _say(message: str) -> None:
    print(f"dist-check: {message}", flush=True)


def _check(condition: bool, message: str) -> None:
    if not condition:
        raise CheckFailed(message)


def _run(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str] | None = None,
    show: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run one command; a non-zero exit fails the check with its output's tail (if ``check``)."""
    _say("$ " + shlex.join(argv))
    result = subprocess.run(
        argv, cwd=cwd, env=env, capture_output=True, text=True, check=False, timeout=900
    )
    if show and result.stdout.strip():
        print(result.stdout.rstrip(), flush=True)
    if check and result.returncode != 0:
        tail = "\n".join((result.stdout + result.stderr).splitlines()[-40:])
        raise CheckFailed(f"`{shlex.join(argv)}` exited {result.returncode}:\n{tail}")
    return result


def _metadata(text: str) -> Message:
    return HeaderParser().parsestr(text)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _child_env() -> dict[str, str]:
    """The environment of the installed commands: none of the caller's Python or git setup.

    ``PYTHON*`` and ``VIRTUAL_ENV`` could point the interpreter back at the
    source tree; ``GIT_*`` and the operator's git configuration (a signing
    requirement, hooks, another default branch) could change what the scratch
    repository does.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTHON", "GIT_")) and key != "VIRTUAL_ENV"
    }
    env["GIT_CONFIG_GLOBAL"] = os.devnull
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


# -- the artifacts ---------------------------------------------------------------
def check_metadata(text: str, where: str, data: dict) -> Message:
    """The core metadata of an sdist (PKG-INFO) or a wheel (METADATA) matches pyproject.toml."""
    project = data["project"]
    meta = _metadata(text)
    raw_version = str(meta.get("Metadata-Version", ""))
    version = tuple(int(part) for part in raw_version.split(".") if part.isdigit())
    _check(
        version >= MIN_METADATA_VERSION,
        f"{where}: Metadata-Version {raw_version!r} is older than 2.4 (PEP 639)",
    )
    expected = {
        "Name": project["name"],
        "Version": project["version"],
        "License-Expression": project["license"],
        "Requires-Python": project["requires-python"],
        "Summary": project["description"],
    }
    for field, value in expected.items():
        _check(meta.get(field) == value, f"{where}: {field} is {meta.get(field)!r}, not {value!r}")
    _check(
        meta.get_all("License-File") == list(project["license-files"]),
        f"{where}: License-File is {meta.get_all('License-File')!r}, "
        f"not {project['license-files']!r}",
    )
    classifiers = meta.get_all("Classifier") or []
    _check(
        classifiers == list(project["classifiers"]),
        f"{where}: the classifiers {classifiers!r} are not pyproject.toml's",
    )
    _check(
        not any(c.startswith("License ::") for c in classifiers),
        f"{where}: a License classifier beside License-Expression (PEP 639 forbids it)",
    )
    _check(meta.get("License") is None, f"{where}: a legacy License field")
    for requirement in meta.get_all("Requires-Dist") or []:
        _check(
            "extra ==" in requirement,
            f"{where}: a runtime dependency {requirement!r}; AutoForge declares none",
        )
    return meta


def check_sdist(path: Path, stem: str, data: dict, root: Path = REPO_ROOT) -> None:
    """The sdist's files, exactly, and its metadata; ``root`` is the tree it was built from."""
    license_bytes = (root / "LICENSE").read_bytes()
    with tarfile.open(path, "r:gz") as archive:
        members = {m.name: m for m in archive.getmembers() if m.isfile()}
        prefix = f"{stem}/"
        stray = sorted(name for name in members if not name.startswith(prefix))
        _check(not stray, f"{path.name}: members outside {prefix}: {stray}")
        names = {name.removeprefix(prefix) for name in members}
        egg_info = f"src/{stem.split('-', 1)[0]}.egg-info/"
        expected = SDIST_TOP_FILES | {f"src/{name}" for name in wheel_package_files(root)}
        found = {name for name in names if not name.startswith(egg_info)}
        local = local_files(found)
        _check(not local, f"{path.name} carries LOCAL mode's files: {local}")
        missing, extra = sorted(expected - found), sorted(found - expected)
        _check(not missing, f"{path.name} is missing {missing}")
        _check(not extra, f"{path.name} carries files it should not: {extra}")

        def read(name: str) -> bytes:
            handle = archive.extractfile(members[prefix + name])
            assert handle is not None
            return handle.read()

        _check(read("LICENSE") == license_bytes, f"{path.name}: LICENSE differs from the tree's")
        check_metadata(read("PKG-INFO").decode("utf-8"), f"{path.name} PKG-INFO", data)
    _say(f"ok: {path.name}: {len(names)} files, metadata and LICENSE as declared")


def check_wheel(path: Path, stem: str, data: dict, root: Path = REPO_ROOT) -> str:
    """The wheel's files, exactly, and its metadata; returns WHEEL's Generator.

    ``root`` is the tree the wheel's sdist was built from.
    """
    dist_info = f"{stem}.dist-info/"
    with zipfile.ZipFile(path) as archive:
        names = {name for name in archive.namelist() if not name.endswith("/")}
        package = {name for name in names if not name.startswith(dist_info)}
        local = local_files(package)
        _check(not local, f"{path.name} carries LOCAL mode's files: {local}")
        expected = wheel_package_files(root)
        missing, extra = sorted(expected - package), sorted(package - expected)
        _check(not missing, f"{path.name} is missing {missing}")
        _check(not extra, f"{path.name} carries files it should not: {extra}")
        info = {name.removeprefix(dist_info) for name in names - package}
        required = {"METADATA", "WHEEL", "RECORD", "entry_points.txt", "licenses/LICENSE"}
        _check(required <= info, f"{path.name}: its dist-info lacks {sorted(required - info)}")
        _check(
            archive.read(dist_info + "licenses/LICENSE") == (root / "LICENSE").read_bytes(),
            f"{path.name}: licenses/LICENSE differs from the tree's LICENSE",
        )
        check_metadata(archive.read(dist_info + "METADATA").decode("utf-8"), path.name, data)
        wheel = _metadata(archive.read(dist_info + "WHEEL").decode("utf-8"))
        _check(
            wheel.get_all("Tag") == ["py3-none-any"], f"{path.name}: tags {wheel.get_all('Tag')}"
        )
        _check(wheel.get("Root-Is-Purelib") == "true", f"{path.name}: not a pure-Python wheel")
        entry_points = configparser.ConfigParser()
        entry_points.read_string(archive.read(dist_info + "entry_points.txt").decode("utf-8"))
        scripts = (
            dict(entry_points["console_scripts"])
            if entry_points.has_section("console_scripts")
            else {}
        )
        _check(
            scripts == dict(data["project"]["scripts"]) == dict([CONSOLE_SCRIPT]),
            f"{path.name}: console scripts {scripts}, expected {dict([CONSOLE_SCRIPT])}",
        )
        generator = str(wheel.get("Generator", "unknown"))
    _say(f"ok: {path.name}: {len(package)} package files, metadata and LICENSE as declared")
    return generator


def copy_source(work: Path) -> Path:
    """The files git sees in the source tree, tracked or untracked but not ignored, copied."""
    git = shutil.which("git")
    _check(git is not None, "git is not on PATH")
    assert git is not None
    listing = ["ls-files", "-z", "--cached", "--others", "--exclude-standard"]
    listed = _run([git, "-C", str(REPO_ROOT), *listing], cwd=REPO_ROOT).stdout
    copy = work / "source"
    copied = 0
    for name in sorted(set(filter(None, listed.split("\0")))):
        source, target = REPO_ROOT / name, copy / name
        if not source.is_symlink() and not source.is_file():
            continue  # deleted in the tree, still in the index
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target, follow_symlinks=False)
        copied += 1
    _say(f"copied the {copied} files of the source tree to {copy}")
    return copy


def build_constraints(root: Path = REPO_ROOT) -> dict[str, tuple[str, tuple[str, ...]]]:
    """``build-constraints.txt``: each pinned name, its version and its SHA-256 hashes.

    Strict: every requirement is ``name==version`` with at least one
    ``--hash=sha256:`` option and nothing else, so ``--require-hashes`` can
    never meet a pin it would resolve freely.
    """
    text = (root / BUILD_CONSTRAINTS).read_text(encoding="utf-8")
    pins: dict[str, tuple[str, tuple[str, ...]]] = {}
    for line in re.sub(r"\\\n", " ", text).splitlines():
        words = line.split("#", 1)[0].split()
        if not words:
            continue
        pin = _PIN.fullmatch(words[0])
        _check(pin is not None, f"{BUILD_CONSTRAINTS}: {words[0]!r} is not name==version")
        assert pin is not None
        hashes = tuple(words[1:])
        _check(
            bool(hashes) and all(_HASH.fullmatch(word) for word in hashes),
            f"{BUILD_CONSTRAINTS}: {pin.group(1)} needs --hash=sha256:<digest> options only",
        )
        name = re.sub(r"[-_.]+", "-", pin.group(1)).lower()
        _check(name not in pins, f"{BUILD_CONSTRAINTS}: {name} is pinned twice")
        pins[name] = (pin.group(2), hashes)
    _check(bool(pins), f"{BUILD_CONSTRAINTS} pins nothing")
    return pins


def build(source: Path, out_dir: Path) -> tuple[Path, Path]:
    """``uv build``: the sdist of ``source``, then a wheel built from it, into ``out_dir``.

    The backend is installed from ``build-constraints.txt`` only, each file
    checked against the hashes there.
    """
    uv = os.environ.get("UV") or shutil.which("uv")
    _check(uv is not None, "uv is not on PATH")
    assert uv is not None
    _run(
        [
            uv,
            "build",
            "--no-config",
            "--python",
            sys.executable,
            "--no-create-gitignore",
            "--build-constraints",
            str(source / BUILD_CONSTRAINTS),
            "--require-hashes",
            "--out-dir",
            str(out_dir),
            str(source),
        ],
        cwd=source,
    )
    data = pyproject(source)
    stem = distribution_stem(data["project"]["name"], data["project"]["version"])
    built = sorted(p.name for p in out_dir.iterdir())
    sdist, wheel = out_dir / f"{stem}.tar.gz", out_dir / f"{stem}-py3-none-any.whl"
    _check(
        built == sorted([sdist.name, wheel.name]),
        f"uv build wrote {built}, expected exactly {sdist.name} and {wheel.name}",
    )
    return sdist, wheel


def install(wheel: Path, work: Path) -> Path:
    """A fresh environment outside the source tree with the wheel installed; its bin/."""
    uv = os.environ.get("UV") or shutil.which("uv")
    assert uv is not None
    venv = work / "venv"
    _run([uv, "venv", "--no-config", "--python", sys.executable, str(venv)], cwd=work)
    python = venv / "bin" / "python"
    _run(
        [
            uv,
            "pip",
            "install",
            "--no-config",
            "--no-cache",
            "--no-index",
            "--python",
            str(python),
            str(wheel),
        ],
        cwd=work,
    )
    return venv / "bin"


def smoke(bin_dir: Path, work: Path, version: str) -> None:
    """The installed command, the installed templates and a REMOTE dry run."""
    env = _child_env()
    autoforge = str(bin_dir / "autoforge")
    out = _run([autoforge, "--version"], cwd=work, env=env).stdout.strip()
    _check(out == f"autoforge {version}", f"`autoforge --version` printed {out!r}")
    _say(f"ok: autoforge --version: {out}")
    help_text = _run([autoforge, "--help"], cwd=work, env=env).stdout
    _check(help_text.startswith("usage: autoforge"), "`autoforge --help` printed no usage")
    _say("ok: autoforge --help")

    _run(
        [
            str(bin_dir / "python"),
            "-I",
            str(Path(__file__).resolve()),
            "--installed",
            "--expect-version",
            version,
            "--source-root",
            str(REPO_ROOT),
        ],
        cwd=work,
        env=env,
        show=True,
    )

    git = shutil.which("git")
    _check(git is not None, "git is not on PATH")
    assert git is not None
    remote_dry_run(autoforge, git, work, env)
    local_refusals(autoforge, git, work, env)


def _scratch_repository(git: str, repo: Path, env: dict[str, str]) -> Path:
    """A new git repository at ``repo`` with one empty commit on ``main``.

    The commit starts no automatic maintenance: since Git 2.47 ``git commit``
    leaves ``git maintenance run --auto --detach`` running, which takes
    ``.git/objects/maintenance.lock`` after the commit has returned, and the
    REMOTE dry run's comparison of the repository before and after it saw
    that as the dry run's write when the host was busy (#340).
    """
    repo.mkdir()
    _run([git, "init", "--quiet", "--initial-branch=main", str(repo)], cwd=repo.parent, env=env)
    _run(
        [
            git,
            "-c",
            "gc.auto=0",
            "-c",
            "maintenance.auto=false",
            "-c",
            "user.name=AutoForge dist check",
            "-c",
            "user.email=dist-check@example.invalid",
            "commit",
            "--quiet",
            "--allow-empty",
            "--no-verify",
            "-m",
            "empty",
        ],
        cwd=repo,
        env=env,
    )
    return repo


class _Entry(NamedTuple):
    """One path as a snapshot holds it: what it holds, which file it is, and its metadata."""

    kind: str  # "file", "dir", "link" or "other"
    content: bytes | str | None  # a file's bytes, a link's target
    device: int
    inode: int
    mode: int
    links: int
    owner: tuple[int, int]
    size: int
    mtime: int  # nanoseconds
    ctime: int  # nanoseconds


def _entry(path: Path) -> _Entry:
    """``path`` itself (``lstat``: a link is not followed), its content and its metadata."""
    st = path.lstat()
    content: bytes | str | None = None
    if stat.S_ISREG(st.st_mode):
        kind, content = "file", path.read_bytes()
    elif stat.S_ISLNK(st.st_mode):
        kind, content = "link", os.readlink(path)
    else:
        kind = "dir" if stat.S_ISDIR(st.st_mode) else "other"
    return _Entry(
        kind,
        content,
        st.st_dev,
        st.st_ino,
        st.st_mode,
        st.st_nlink,
        (st.st_uid, st.st_gid),
        st.st_size,
        st.st_mtime_ns,
        st.st_ctime_ns,
    )


def _tree(root: Path) -> dict[str, _Entry]:
    """``root`` (as ``.``) and every path under it, ``.git`` included, as :func:`_entry` reads it.

    What a write changes, besides a path's bytes (PR #425 R1-F4): a file
    rewritten in place with its own bytes has a new ``mtime`` and
    ``ctime``; one replaced by a copy (a rename over it) is another inode,
    and its directory has a new ``mtime``; a ``chmod``, a ``chown`` or a
    ``touch`` that puts the old times back still sets ``ctime``, which no
    unprivileged call sets back; a file created and removed again, a lock
    file among them, leaves its directory a new ``mtime``. ``atime`` is left
    out, since reading a file may set it.

    What it cannot see: a change outside ``root``; a lock taken with
    ``flock`` or ``fcntl`` on a file that already exists, which changes no
    metadata (no lock file exists before a refusal check runs, so taking
    the controller lock would create one); a write within one tick of the
    kernel's timestamp clock of the snapshot before it, which a command's
    own start-up outlasts; and a write made after the snapshot after it, by
    a process the command left running.
    """
    return {
        ".": _entry(root),
        **{str(path.relative_to(root)): _entry(path) for path in sorted(root.rglob("*"))},
    }


def _changes(before: dict[str, _Entry], after: dict[str, _Entry]) -> list[str]:
    """Each path that differs between two snapshots, with what differs about it."""
    found: list[str] = []
    for path in sorted(before.keys() | after.keys()):
        old, new = before.get(path), after.get(path)
        if old == new:
            continue
        if old is None:
            found.append(f"{path} (created)")
        elif new is None:
            found.append(f"{path} (removed)")
        else:
            fields = [name for name, a, b in zip(_Entry._fields, old, new, strict=True) if a != b]
            found.append(f"{path} ({', '.join(fields)})")
    return found


def _fake_gh(fake_bin: Path, calls: Path) -> None:
    """A ``gh`` in ``fake_bin`` that records its arguments in ``calls``.

    It answers the EPIC binding's two reads (:data:`_BINDING_ANSWERS`, matched
    on the whole argument list) and fails every other call.
    """
    fake_bin.mkdir()
    fake = fake_bin / "gh"
    cases = "".join(
        f"  {shlex.quote(args)}) printf '%s\\n' {shlex.quote(answer)}; exit 0 ;;\n"
        for args, answer in _BINDING_ANSWERS.items()
    )
    fake.write_text(
        "#!/bin/sh\n"
        f'printf "%s\\n" "$*" >> {shlex.quote(str(calls))}\n'
        f'case "$*" in\n{cases}esac\n'
        "exit 1\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)


def _gh_calls(calls: Path) -> str:
    return calls.read_text(encoding="utf-8") if calls.exists() else ""


def remote_dry_run(autoforge: str, git: str, work: Path, env: dict[str, str]) -> None:
    """A REMOTE ``run --dry-run`` binds, renders a prompt, reads only the binding, writes nothing.

    The repository has no ``origin`` and the issue URLs name no real
    repository: past the EPIC binding's two reads (#293), which the ``gh``
    first on ``PATH`` answers, a dry run plans from in-memory state alone.
    That ``gh`` records its arguments and fails any other call, so any other
    call shows.
    """
    calls = work / "gh-calls.log"
    _fake_gh(work / "fake-gh-bin", calls)
    remote_env = dict(env, PATH=f"{work / 'fake-gh-bin'}{os.pathsep}{env.get('PATH', '')}")
    repo = _scratch_repository(git, work / "remote-repo", remote_env)
    _dry_run(autoforge, repo, remote_env, calls)
    _say(
        "ok: autoforge run --dry-run --full-prompt bound the EPIC and rendered the "
        "ANALYZE_EXECUTE prompt; gh read the issue and the EPIC, nothing else"
    )


# LOCAL mode's ways in, and the generic refusal each meets in the installed
# command (ADR 0009 D3 to D5): the words after `autoforge`, the exit status, and
# what its stderr must say. `doctor`'s refusal of a `local:` key, its failing
# `config` row, is tests/test_cli.py's: doctor runs `gh` and the agent CLIs.
# `step`, `resume` and `unblock` come twice: as dry runs, which load the state
# without the lock, and as the ordinary invocations, which refuse it before
# the lock and would otherwise take the lock, write and launch (PR #425
# R1-F3); `run` over the state is refused before the lock as well.
_LOCAL_WAYS_IN: tuple[tuple[tuple[str, ...], int, str], ...] = (
    (("local",), 2, "invalid choice: 'local'"),
    (("local", "run", "features/add-filter.md"), 2, "invalid choice: 'local'"),
    (("local", "doctor"), 2, "invalid choice: 'local'"),
    (("--config", "{config}", "status"), 2, "unknown key(s) under 'the top level': local"),
    (("status",), 2, "state file has unknown mode 'LOCAL'"),
    (("status", "--json"), 2, "state file has unknown mode 'LOCAL'"),
    (("--state-dir", "{state_dir}", "status"), 2, "state file has unknown mode 'LOCAL'"),
    (("step", "--dry-run"), 2, "state file has unknown mode 'LOCAL'"),
    (("resume", "--dry-run"), 2, "state file has unknown mode 'LOCAL'"),
    (("unblock", "--reason", "looked", "--dry-run"), 2, "state file has unknown mode 'LOCAL'"),
    (("step",), 2, "state file has unknown mode 'LOCAL'"),
    (("resume",), 2, "state file has unknown mode 'LOCAL'"),
    (("unblock", "--reason", "looked"), 2, "state file has unknown mode 'LOCAL'"),
    (
        ("run", "--epic", REMOTE_EPIC, "--issue", REMOTE_ISSUE),
        2,
        "state file has unknown mode 'LOCAL'",
    ),
)
# What a regression could reach instead of refusing: GitHub through `gh`, a
# model through an agent CLI. Each is a fake first on PATH that records its
# call and fails it.
_RECORDED_EXECUTABLES = ("gh", "claude", "opencode", "pi")


def _isolated_env(env: dict[str, str], fake_bin: Path, home: Path, tmp: Path) -> dict[str, str]:
    """An environment of the refusal check's own: only what the installed command needs.

    ``PATH`` with ``fake_bin`` first, so ``git`` is the host's and every
    executable of :data:`_RECORDED_EXECUTABLES` a recording fake; a scratch
    ``HOME``, XDG directories inside it and ``TMPDIR``; no git configuration
    of the operator's. No variable of the caller's environment but ``PATH``
    reaches it, so no credential, provider key or ``gh`` configuration does.
    """
    return {
        "PATH": f"{fake_bin}{os.pathsep}{env.get('PATH', os.defpath)}",
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "TMPDIR": str(tmp),
        "LANG": "C.UTF-8",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }


def local_refusals(autoforge: str, git: str, work: Path, env: dict[str, str]) -> None:
    """LOCAL mode's ways in meet the installed command's generic refusals (ADR 0009, #389).

    A scratch repository holds what an operator upgrading from a release
    with LOCAL mode may have: a LOCAL state, with a run log, at the default
    ``state_dir``, a feature file, and a configuration file beside it that
    still has a ``local:`` block. ``--help`` names no ``local``; each way in
    of :data:`_LOCAL_WAYS_IN` exits as its row says, naming its refusal.
    Every command runs in :func:`_isolated_env`: a regression that took the
    lock and stepped would meet a fake ``gh`` or agent CLI, never GitHub or
    a model. Afterwards no fake has been called, and the repository (its
    state directory and ``.git`` included), the configuration file, the
    scratch ``HOME`` and ``TMPDIR`` are as they were, bytes, identity and
    metadata (:func:`_tree`, which says what that cannot see).
    """
    fake_bin = work / "fake-bin-local"
    fake_bin.mkdir()
    calls = work / "calls-local.log"
    for name in _RECORDED_EXECUTABLES:
        fake = fake_bin / name
        fake.write_text(
            f'#!/bin/sh\nprintf "%s %s\\n" {name} "$*" >> {shlex.quote(str(calls))}\nexit 1\n',
            encoding="utf-8",
        )
        fake.chmod(0o755)
    home, tmp = work / "local-home", work / "local-tmp"
    for directory in (".config", ".cache", ".local/share", ".local/state"):
        (home / directory).mkdir(parents=True)
    tmp.mkdir()
    local_env = _isolated_env(env, fake_bin, home, tmp)
    help_text = _run([autoforge, "--help"], cwd=work, env=local_env).stdout
    _check("local" not in help_text.lower(), "`autoforge --help` names `local`")
    repo = _scratch_repository(git, work / "local-repo", local_env)
    state_dir = repo / ".autoforge"
    (state_dir / "logs" / "local-run").mkdir(parents=True)
    (state_dir / "state.json").write_text(
        json.dumps({"mode": "LOCAL", "protocol_version": "12", "phase": "REVIEW"}),
        encoding="utf-8",
    )
    (state_dir / "logs" / "local-run" / "run.log").write_text("a LOCAL run's log\n")
    (repo / "features").mkdir()
    (repo / "features" / "add-filter.md").write_text("# Feature: add filter\n")
    config = work / "local-config.json"
    config.write_text(json.dumps({"version": 1, "local": {"max_fix_rounds": 1}}))
    roots = {"the scratch repository": repo, "the scratch HOME": home, "the scratch TMPDIR": tmp}

    def snapshot() -> dict[str, dict[str, _Entry]]:
        return {
            **{name: _tree(root) for name, root in roots.items()},
            f"the configuration file {config}": {config.name: _entry(config)},
        }

    before = snapshot()
    for words, status, refusal in _LOCAL_WAYS_IN:
        argv = [
            autoforge,
            *(word.format(config=config, state_dir=state_dir) for word in words),
        ]
        _say("$ " + shlex.join(argv))
        result = subprocess.run(
            argv, cwd=repo, env=local_env, capture_output=True, text=True, check=False, timeout=300
        )
        _check(
            result.returncode == status and refusal in result.stderr,
            f"`{shlex.join(argv)}` exited {result.returncode}, not {status} naming {refusal!r}:\n"
            + "\n".join((result.stdout + result.stderr).splitlines()[-20:]),
        )
        if calls.exists():
            raise CheckFailed(f"`{shlex.join(argv)}` called {calls.read_text('utf-8')!r}")
        after = snapshot()
        for name, entries in before.items():
            changed = _changes(entries, after[name])
            _check(not changed, f"`{shlex.join(argv)}` changed {name}: {changed}")
    _say(
        f"ok: LOCAL mode's {len(_LOCAL_WAYS_IN)} ways in met their generic refusals, "
        "no gh or agent CLI called, nothing written"
    )


def _dry_run(
    autoforge: str, repo: Path, env: dict[str, str], calls: Path, options: tuple[str, ...] = ()
) -> None:
    """``run --dry-run --full-prompt`` in ``repo``: the binding, the prompt, no write.

    ``gh`` is called for the EPIC binding's two reads and nothing else (#293).
    """
    before, called = _tree(repo), _gh_calls(calls)
    argv = [autoforge, *options, "run", "--epic", REMOTE_EPIC, "--issue", REMOTE_ISSUE]
    plan = _run([*argv, "--dry-run", "--full-prompt"], cwd=repo, env=env).stdout
    for needle in (
        f"  used:          #1 {REMOTE_EPIC} (the issue's native parent, which --epic names too)",
        "Template: analyze_execute.md",
        "--- prompt preview ---",
        _CONTROL_RESULT,
    ):
        _check(needle in plan, f"the REMOTE dry run printed no {needle!r}")
    _check(not _PLACEHOLDER.search(plan), "the REMOTE dry run's prompt has an unrendered variable")
    made = _gh_calls(calls)[len(called) :].splitlines()
    if made != list(_BINDING_ANSWERS):
        raise CheckFailed(
            f"the REMOTE dry run called gh for {made!r}, not for the EPIC binding's two reads "
            f"{list(_BINDING_ANSWERS)!r} alone"
        )
    changed = _changes(before, _tree(repo))
    _check(not changed, f"the REMOTE dry run changed the scratch repository: {changed}")


def check_first_run_doctor(report: dict, agents: frozenset[str], what: str) -> None:
    """``doctor --json`` before anything is signed in: what needs no credential is OK.

    Every row of :data:`CREDENTIAL_FREE_ROWS` is there and OK, the merge gate
    is closed, and the agent rows name exactly the CLIs of ``agents``: the
    ones the configuration routes a profile to. The rows that need ``gh``,
    a sign-in or an agent CLI may say anything.
    """
    rows = {check["name"]: check for check in report.get("checks", [])}
    for name in CREDENTIAL_FREE_ROWS:
        row = rows.get(name)
        _check(row is not None, f"doctor with {what} printed no {name!r} row")
        assert row is not None
        _check(
            row["ok"] and not row["skipped"],
            f"doctor with {what}: {name!r} is not OK: {row['detail']}",
        )
    gate = rows["merge gate"]["detail"]
    _check(gate.startswith("CLOSED"), f"doctor with {what}: the merge gate is not closed: {gate}")
    # The installed command runs the installed package, never this checkout's
    # source tree (#265: the controller row names what was imported).
    source = str(REPO_ROOT / "src" / "autoforge")
    controller = rows["controller"]["detail"]
    _check(
        source not in controller,
        f"doctor with {what}: the installed autoforge imports the source tree: {controller}",
    )
    named = {match.group(1) for name in rows if (match := _AGENT_ROW.fullmatch(name))}
    _check(
        named == agents,
        f"doctor with {what} checked the agent CLIs {sorted(named)}, expected {sorted(agents)}",
    )


def first_run(wheel: Path, examples: Path, work: Path, version: str) -> None:
    """The offline half of the first run docs/getting-started.md walks through (#259).

    ``uv tool install`` puts the wheel's ``autoforge`` on ``PATH``, as the
    guide installs it but from the file alone, with a home and tool
    directories of its own. Then, with only that home, git, the installed
    command and a ``gh`` that records and fails every call on ``PATH`` (no
    credential, no agent CLI): ``autoforge --version`` and ``--help`` outside
    any repository; and, in a repository whose ``origin`` is on GitHub and
    which keeps AutoForge's files out of git as the guide says, ``doctor
    --json`` with the built-in routing and with each starter of
    ``examples/`` saved as ``autoforge.yaml`` (see
    :func:`check_first_run_doctor`), and a REMOTE dry run with each starter.
    The repository's ``git status`` stays empty.
    """
    uv = os.environ.get("UV") or shutil.which("uv")
    git = shutil.which("git")
    assert uv is not None and git is not None
    _check(
        sorted(p.name for p in examples.glob("*.yaml")) == sorted(STARTER_AGENTS),
        f"examples/ holds {sorted(p.name for p in examples.glob('*.yaml'))}, "
        f"STARTER_AGENTS names {sorted(STARTER_AGENTS)}",
    )
    home, tool_bin = work / "first-run-home", work / "first-run-bin"
    home.mkdir()
    uv_env = {
        key: value for key, value in _child_env().items() if not key.startswith(("UV_", "XDG_"))
    }
    uv_env.update(HOME=str(home), UV_TOOL_DIR=str(work / "uv-tools"), UV_TOOL_BIN_DIR=str(tool_bin))
    install_argv = ["tool", "install", "--no-config", "--no-cache", "--no-index", "--offline"]
    _run([uv, *install_argv, "--python", sys.executable, str(wheel)], cwd=home, env=uv_env)

    calls = work / "first-run-gh-calls.log"
    _fake_gh(work / "first-run-gh-bin", calls)
    path = [str(work / "first-run-gh-bin"), str(tool_bin), str(Path(git).parent), os.defpath]
    env = {
        "HOME": str(home),
        "PATH": os.pathsep.join(path),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if os.environ.get("TMPDIR"):
        env["TMPDIR"] = os.environ["TMPDIR"]
    found = shutil.which("autoforge", path=env["PATH"])
    _check(found == str(tool_bin / "autoforge"), f"`autoforge` on PATH is {found}")
    out = _run(["autoforge", "--version"], cwd=home, env=env).stdout.strip()
    _check(out == f"autoforge {version}", f"`autoforge --version` printed {out!r}")
    help_text = _run(["autoforge", "--help"], cwd=home, env=env).stdout
    _check(help_text.startswith("usage: autoforge"), "`autoforge --help` printed no usage")
    _say(f"ok: uv tool install put {out} on PATH")

    repo = _scratch_repository(git, work / "first-run-repo", env)
    _run([git, "remote", "add", "origin", FIRST_RUN_ORIGIN], cwd=repo, env=env)
    exclude = repo / ".git" / "info" / "exclude"
    exclude.parent.mkdir(exist_ok=True)
    with exclude.open("a", encoding="utf-8") as handle:
        handle.write("".join(f"{entry}\n" for entry in FIRST_RUN_EXCLUDES))
    for name, agents in [(None, BUILT_IN_AGENTS), *sorted(STARTER_AGENTS.items())]:
        what = name or "the built-in routing"
        options: tuple[str, ...] = ()
        if name is not None:
            shutil.copyfile(examples / name, repo / "autoforge.yaml")
            options = ("--config", "autoforge.yaml")
        doctor = _run(["autoforge", *options, "doctor", "--json"], cwd=repo, env=env, check=False)
        _check(
            doctor.returncode in (0, 1),
            f"doctor with {what} exited {doctor.returncode}: {doctor.stderr[-2000:]}",
        )
        try:
            report = json.loads(doctor.stdout)
        except ValueError as exc:
            raise CheckFailed(f"doctor --json with {what} printed no JSON: {exc}") from None
        check_first_run_doctor(report, agents, what)
        _say(f"ok: doctor with {what}: every row that needs no credential is OK")
        if name is not None:
            _dry_run("autoforge", repo, env, calls, options)
            _say(f"ok: autoforge run --dry-run with {name} rendered the ANALYZE_EXECUTE prompt")
    status = _run([git, "status", "--porcelain", "--untracked-files=all"], cwd=repo, env=env)
    _check(not status.stdout.strip(), f"the first run left files git sees: {status.stdout!r}")
    _say("ok: the first run's files stay out of git")


def check_release_tag(tag: str, version: str) -> None:
    """A release is built only from the tag ``v<version>`` of a version the procedure allows."""
    _check(
        RELEASE_VERSION.fullmatch(version) is not None,
        f"the version {version!r} is not a release version X.Y.Z[aN|bN|rcN]: a .devM "
        "version is never released (docs/releasing.md)",
    )
    _check(
        tag == f"v{version}",
        f"the tag {tag!r} does not name the package's version {version!r}: it must be 'v{version}'",
    )


def record_digests(paths: list[Path], sha256sums: Path | None, version: str) -> None:
    lines = [f"{_sha256(path)}  {path.name}" for path in paths]
    for line in lines:
        _say(f"sha256 {line}")
    if sha256sums is not None:
        sha256sums.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
        _say(f"wrote {sha256sums}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        python = ".".join(str(part) for part in sys.version_info[:3])
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(f"### Distributions (CPython {python})\n\n```text\n")
            handle.write("".join(f"{line}\n" for line in lines))
            handle.write("```\n\n")
    # The step's outputs, which pypi-publish.yml hands from its build job to its
    # publish job: the version and the digests that job re-verifies.
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as handle:
            handle.write(f"version={version}\n")
            handle.write("sha256sums<<SHA256SUMS_END\n")
            handle.write("".join(f"{line}\n" for line in lines))
            handle.write("SHA256SUMS_END\n")


def check_distributions(
    out_dir: Path | None, sha256sums: Path | None, release_tag: str | None = None
) -> None:
    data = pyproject()
    name, version = data["project"]["name"], data["project"]["version"]
    if release_tag is not None:
        check_release_tag(release_tag, version)
        _say(f"ok: the tag {release_tag} names the version {version}")
    work = Path(tempfile.mkdtemp(prefix="autoforge-dist-check-")).resolve()
    _check(
        not work.is_relative_to(REPO_ROOT),
        f"the scratch directory {work} is inside the source tree; point TMPDIR elsewhere",
    )
    _say(f"{name} {version} on {sys.executable} (Python {sys.version.split()[0]}); scratch {work}")
    try:
        # The tools' versions, for the record of the run.
        for tool in (os.environ.get("UV") or "uv", "git"):
            _say(_run([tool, "--version"], cwd=work).stdout.strip())
        target = out_dir.resolve() if out_dir is not None else work / "dist"
        _check(
            not target.exists() or not any(target.iterdir()),
            f"the output directory {target} is not empty",
        )
        target.mkdir(parents=True, exist_ok=True)
        source = copy_source(work)
        _check(pyproject(source) == data, "pyproject.toml changed while it was copied")
        sdist, wheel = build(source, target)
        stem = distribution_stem(name, version)
        check_sdist(sdist, stem, data, source)
        generator = check_wheel(wheel, stem, data, source)
        backend = build_constraints(source)["setuptools"][0]
        _check(
            generator == f"setuptools ({backend})",
            f"the wheel was built by {generator!r}, not the pinned setuptools {backend}",
        )
        _say(f"built by {generator}, the version {BUILD_CONSTRAINTS} pins")
        bin_dir = install(wheel, work)
        _check(not bin_dir.is_relative_to(REPO_ROOT), "the environment is inside the source tree")
        smoke(bin_dir, work, version)
        first_run(wheel, source / "examples", work, version)
        record_digests([sdist, wheel], sha256sums, version)
    except CheckFailed:
        _say(f"FAILED; the scratch directory {work} is kept")
        raise
    shutil.rmtree(work)
    _say(f"passed: {sdist.name} and {wheel.name}" + (f" are in {target}" if out_dir else ""))


# -- inside the installed environment ---------------------------------------------
def check_installed(expect_version: str, source_root: Path) -> None:
    """Run by the environment's own interpreter: the package it imports is the installed one."""
    import importlib.metadata
    import importlib.util

    import autoforge
    from autoforge import engine, prompts

    prefix = Path(sys.prefix).resolve()
    location = Path(autoforge.__file__).resolve()
    _check(
        location.is_relative_to(prefix) and not location.is_relative_to(source_root.resolve()),
        f"autoforge was imported from {location}, not from the environment {prefix}",
    )
    dist = importlib.metadata.distribution("autoforge-cli")
    _check(dist.version == expect_version, f"installed version {dist.version}")
    _check(
        autoforge.__version__ == expect_version, f"autoforge.__version__ {autoforge.__version__}"
    )
    direct_url = dist.read_text("direct_url.json")
    if direct_url:
        editable = json.loads(direct_url).get("dir_info", {}).get("editable", False)
        _check(not editable, "autoforge-cli is installed editable")
    scripts = [(ep.name, ep.value) for ep in dist.entry_points if ep.group == "console_scripts"]
    _check(scripts == [CONSOLE_SCRIPT], f"installed console scripts {scripts}")

    directory = prompts.prompts_dir().resolve()
    _check(directory.is_relative_to(prefix), f"templates read from {directory}")
    on_disk = sorted(path.name for path in directory.glob("*.md"))
    registered = sorted(prompts.TEMPLATE_FILES)
    _check(on_disk == registered, f"installed templates {on_disk}, registered {registered}")
    installed = {
        path.relative_to(location.parent.parent).as_posix()
        for path in location.parent.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    }
    local = local_files(
        installed | {f"autoforge/prompts/{name}" for name in {*on_disk, *registered}}
    )
    _check(not local, f"the installed package carries LOCAL mode's files: {local}")
    importable = [name for name in LOCAL_MODULES if importlib.util.find_spec(name) is not None]
    _check(not importable, f"LOCAL mode's modules import from the installed package: {importable}")
    _check(engine.check_template_files() == [], f"missing: {engine.check_template_files()}")

    def variables(*names: str) -> dict[str, str | int | None]:
        texts = [prompts.load_template(name) for name in names]
        return {var: f"<{var}>" for text in texts for var in prompts.required_variables(text)}

    def checked(text: str, what: str, phase: bool) -> None:
        _check(bool(text.strip()), f"{what} rendered empty")
        _check(not _PLACEHOLDER.search(text), f"{what} left a variable unrendered")
        _check(not phase or _CONTROL_RESULT in text, f"{what} asks for no CONTROL_RESULT")

    rendered: set[str] = set()
    common = prompts.COMMON_TEMPLATE
    for phase, name in engine.PHASE_TEMPLATE.items():
        if name is None:
            continue
        text = prompts.render_phase(name, variables(common, name), common_template=common)
        checked(text, f"{phase.value} ({common} + {name})", phase=True)
        rendered |= {common, name}
    rerequest = engine.UPDATE_EPIC_RE_REQUEST_TEMPLATE
    text = prompts.render_phase(rerequest, variables(prompts.COMMON_TEMPLATE, rerequest))
    checked(text, f"UPDATE_EPIC re-request ({rerequest})", phase=False)
    rendered.add(rerequest)
    for name in sorted(set(prompts.TEMPLATE_FILES) - rendered):
        checked(prompts.render(prompts.load_template(name), variables(name)), name, phase=False)
        rendered.add(name)
    _check(rendered == set(prompts.TEMPLATE_FILES), "not every template was rendered")
    _say(f"ok: rendered all {len(rendered)} prompt templates from {directory}")
    _say(f"ok: {dist.metadata['Name']} {dist.version} imported from {location.parent}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out-dir", type=Path, help="keep the sdist and the wheel here")
    parser.add_argument("--sha256sums", type=Path, help="write the artifacts' SHA-256 here")
    parser.add_argument(
        "--release-tag",
        help="build a release: refuse unless this tag is 'v' and the package's version",
    )
    parser.add_argument("--installed", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--expect-version", help=argparse.SUPPRESS)
    parser.add_argument("--source-root", type=Path, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    try:
        if args.installed:
            check_installed(args.expect_version, args.source_root)
        else:
            check_distributions(args.out_dir, args.sha256sums, args.release_tag)
    except CheckFailed as exc:
        print(f"dist-check: FAILED: {exc}", file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
