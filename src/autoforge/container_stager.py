"""The stager: writes a container launch's ``/workspace`` from its seed, in the stager container.

ADR 0013 D2 (the container side), D12 and D13's stage step (#271). It runs
in the launch's *stager container*, offline (``--network none``) and with
no credential, as the agent's UID, before the prover and before the launch
container starts. Its verdict is one JSON line on standard output.

It imports nothing of :mod:`autoforge` and only the standard library, so
the image carries this file alone and runs it with ``python3 -I``. Two
commands:

- ``receive`` (the copy-in form only): standard input is the seed pack,
  written exclusively to :data:`RECEIVED_PACK` in the stager container's
  private ``/tmp``. The copy-in's transport is this helper's standard
  input, not the runtime's ``cp``, which cannot write into a tmpfs and
  which would put the pack in the workspace volume.
- ``stage``: standard input is the request, one JSON object
  (:func:`parse_request`): the launch's role (``agent`` or ``export``),
  the staging form (``bind`` or ``copy``), the object format, and the
  seed's manifest with its digest, which the controller checked against
  the launch's git input before the hand-off. The stager checks the
  manifest's bytes against that digest and the pack against the
  manifest's size and SHA-256, so a seed changed after the hand-off is
  refused here too.

**For an agent** ``/workspace`` becomes a self-contained repository: ``git
init`` with an empty template (no hook, no sample file), the configuration
replaced by exactly :func:`config_text` (``logallrefupdates`` off, so no
reflog is written), ``.git/shallow`` naming the manifest's boundaries,
``index-pack --strict`` of the pack, the manifest's tag refs and no other
ref, and ``HEAD`` detached at the seed commit (the carried commit, for a
relaunch) with its tree written by ``read-tree -u``.

**For an export command** ``/workspace`` is the commit's tree with no
``.git``: in the bind form a copy of ``/source/tree``, the tree the host
materialized and proved; in the copy-in form written from the pack through
a private git directory in ``/tmp``, removed afterwards.

**No filter, attribute rewrite or hook runs** (D2, required change C).
Every git process runs with no system or global configuration
(``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL=/dev/null``), no system or
global attributes file, a private empty ``HOME``, hooks pointed at
``/dev/null``, ``core.fsmonitor`` off and LFS smudging off. For the
checkout, ``$GIT_DIR/info/attributes`` (which overrides the tree's
``.gitattributes``) unsets ``filter``, ``ident``, ``text``, ``eol``,
``crlf`` and ``working-tree-encoding`` for every path, and is removed
after it, so every file is its blob's bytes: an LFS pointer stays a
pointer, a gitlink is an empty directory, and neither the image's git
configuration nor the repository's attributes can rewrite a byte. A
rewrite that got through anyway would fail the prover's tree proof.

Nothing here decides whether the result is right: the prover does, from
the tree id and ref set the controller passes it, never from this
helper's verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from typing import BinaryIO

SOURCE = "/source"
WORKSPACE = "/workspace"
# The copy-in form's received seed, in the stager container's private /tmp.
RECEIVE_DIR = "/tmp/autoforge-seed"
RECEIVED_PACK = RECEIVE_DIR + "/seed.pack"
# The bind form's staged seed and tree (``autoforge.source_staging``).
STAGED_SEED = "seed"
STAGED_TREE = "tree"
SEED_PACK = "seed.pack"
SEED_MANIFEST = "manifest.json"

ROLES = ("agent", "export")
FORMS = ("bind", "copy")
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PACK_BYTES = 64 * 1024 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 1800
_CHUNK = 1024 * 1024

_OID = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TAG_REF = re.compile(r"refs/tags/[A-Za-z0-9._/+@-]{1,200}")
# Every attribute that converts a file between its blob and the working
# tree, unset for every path for the checkout (D2).
CHECKOUT_ATTRIBUTES = b"* -filter -ident -text -eol -crlf -working-tree-encoding\n"
# What every git process gets on its command line: never written to a file.
GIT_OPTIONS = (
    "-c",
    "core.hooksPath=/dev/null",
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.attributesFile=/dev/null",
    "-c",
    "core.autocrlf=false",
    "-c",
    "core.symlinks=true",
    "-c",
    "protocol.allow=never",
    "-c",
    "advice.detachedHead=false",
)


class StageError(Exception):
    """A staging failure: the launch's setup failed, and no agent or command runs."""


def config_text(object_format: str) -> str:
    """The seeded repository's whole configuration: the stager writes exactly this."""
    version = 1 if object_format == "sha256" else 0
    text = (
        "[core]\n"
        f"\trepositoryformatversion = {version}\n"
        "\tfilemode = true\n"
        "\tbare = false\n"
        "\tlogallrefupdates = false\n"
    )
    if object_format == "sha256":
        text += "[extensions]\n\tobjectformat = sha256\n"
    return text


@dataclass(frozen=True)
class Paths:
    """Where the stager reads and writes: the container's fixed places (a test's own)."""

    source: str = SOURCE
    workspace: str = WORKSPACE
    received_pack: str = RECEIVED_PACK
    tmp: str = "/tmp"


DEFAULT_PATHS = Paths()


@dataclass(frozen=True)
class Manifest:
    """What the stager needs of the seed's manifest, parsed from its checked bytes."""

    object_format: str
    seed: str
    carried: str
    boundaries: tuple[str, ...]
    tags: tuple[tuple[str, str], ...]
    pack_bytes: int
    pack_sha256: str
    export_command: bool

    @property
    def head(self) -> str:
        return self.carried or self.seed


@dataclass(frozen=True)
class Request:
    role: str
    form: str
    object_format: str
    manifest: Manifest


def _oid(value: object, object_format: str, what: str) -> str:
    pattern = _OID.get(object_format)
    if not isinstance(value, str) or pattern is None or not pattern.fullmatch(value):
        raise StageError(f"{what} is not a full object id")
    return value


def parse_manifest(raw: bytes, digest: str) -> Manifest:
    """The manifest's bytes, checked against ``digest``, then the fields staging uses."""
    if not _DIGEST.fullmatch(digest):
        raise StageError("the request's manifest digest is not a sha256 digest")
    if "sha256:" + hashlib.sha256(raw).hexdigest() != digest:
        raise StageError("the seed's manifest does not hash to the digest the controller bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise StageError("the seed's manifest does not parse") from None
    if not isinstance(data, dict):
        raise StageError("the seed's manifest is not an object")
    object_format = data.get("object_format")
    if object_format not in _OID:
        raise StageError("the seed's manifest names no known object format")
    seed = _oid(data.get("seed"), object_format, "the manifest's seed")
    carried = data.get("carried")
    if carried != "":
        carried = _oid(carried, object_format, "the manifest's carried commit")
    boundaries = data.get("boundaries")
    if not isinstance(boundaries, list):
        raise StageError("the manifest's boundaries are not a list")
    bounds = tuple(_oid(b, object_format, "a manifest boundary") for b in boundaries)
    tags: list[tuple[str, str]] = []
    raw_tags = data.get("tags")
    if not isinstance(raw_tags, list):
        raise StageError("the manifest's tags are not a list")
    for tag in raw_tags:
        if not isinstance(tag, dict) or set(tag) != {"ref", "object"}:
            raise StageError("a manifest tag is not a ref and an object")
        ref = tag["ref"]
        if not isinstance(ref, str) or not _TAG_REF.fullmatch(ref) or ".." in ref:
            raise StageError("a manifest tag's ref is not a plain tag ref")
        tags.append((ref, _oid(tag["object"], object_format, "a manifest tag's object")))
    size = data.get("pack_bytes")
    sha = data.get("pack_sha256")
    if type(size) is not int or size < 0 or size > MAX_PACK_BYTES:
        raise StageError("the manifest's pack size is not a bounded byte count")
    if not isinstance(sha, str) or not re.fullmatch(r"[0-9a-f]{64}", sha):
        raise StageError("the manifest's pack_sha256 is not a SHA-256")
    export = data.get("export_command")
    if type(export) is not bool:
        raise StageError("the manifest's export_command is not a boolean")
    return Manifest(object_format, seed, carried, bounds, tuple(tags), size, sha, export)


def parse_request(raw: bytes) -> Request:
    """The ``stage`` request: exact keys, a known role and form, a manifest matching its digest."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise StageError("the request is over its bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise StageError("the request does not parse") from None
    keys = {"role", "form", "object_format", "manifest", "manifest_digest"}
    if not isinstance(data, dict) or set(data) != keys:
        raise StageError("the request does not hold exactly its fields")
    role, form, object_format = data["role"], data["form"], data["object_format"]
    if role not in ROLES or form not in FORMS or object_format not in _OID:
        raise StageError("the request names an unknown role, form or object format")
    if not isinstance(data["manifest"], str) or not isinstance(data["manifest_digest"], str):
        raise StageError("the request's manifest is not text")
    manifest = parse_manifest(data["manifest"].encode("utf-8"), data["manifest_digest"])
    if manifest.object_format != object_format:
        raise StageError("the manifest's object format is not the request's")
    if manifest.export_command != (role == "export"):
        raise StageError("the manifest's export_command does not match the launch's role")
    return Request(role, form, object_format, manifest)


# -- git ------------------------------------------------------------------------------


def git_environment(home: str) -> dict[str, str]:
    """A git process's whole environment: no configuration, attributes or credential of anyone's."""
    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "XDG_CONFIG_HOME": home,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_LFS_SKIP_SMUDGE": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }


class Git:
    """git as the stager runs it: :data:`GIT_OPTIONS`, :func:`git_environment`, no other stdin."""

    def __init__(self, home: str) -> None:
        self.env = git_environment(home)

    def run(
        self,
        args: list[str],
        *,
        cwd: str,
        git_dir: str | None = None,
        work_tree: str | None = None,
        stdin: BinaryIO | None = None,
    ) -> bytes:
        env = dict(self.env)
        if git_dir is not None:
            env["GIT_DIR"] = git_dir
        if work_tree is not None:
            env["GIT_WORK_TREE"] = work_tree
        try:
            proc = subprocess.run(
                ["git", *GIT_OPTIONS, *args],
                cwd=cwd,
                env=env,
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise StageError(f"git {args[0]} could not run ({type(exc).__name__})") from None
        if proc.returncode != 0:
            detail = proc.stderr.decode("utf-8", "replace").strip().splitlines()
            tail = detail[-1][:300] if detail else ""
            raise StageError(f"git {args[0]} exited {proc.returncode}: {tail}")
        return proc.stdout


# -- the steps ------------------------------------------------------------------------


def receive(stream: BinaryIO, paths: Paths = DEFAULT_PATHS) -> dict[str, object]:
    """The copy-in form's seed pack, from ``stream`` into :data:`RECEIVED_PACK`, exclusively."""
    os.makedirs(os.path.dirname(paths.received_pack), mode=0o700, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    try:
        fd = os.open(paths.received_pack, flags, 0o600)
    except OSError as exc:
        raise StageError(f"the received pack cannot be created ({exc.strerror})") from None
    digest = hashlib.sha256()
    size = 0
    with os.fdopen(fd, "wb") as out:
        while True:
            chunk = stream.read(_CHUNK)
            if not chunk:
                break
            size += len(chunk)
            if size > MAX_PACK_BYTES:
                raise StageError("the received pack is over its bound")
            digest.update(chunk)
            out.write(chunk)
    return {"stager": "received", "bytes": size, "sha256": digest.hexdigest()}


def _pack_path(request: Request, paths: Paths) -> str:
    if request.form == "bind":
        return os.path.join(paths.source, STAGED_SEED, SEED_PACK)
    return paths.received_pack


def _check_pack(path: str, manifest: Manifest) -> None:
    """The pack is a regular file of the manifest's size and SHA-256."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise StageError(f"the seed pack cannot be opened ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise StageError("the seed pack is not a regular file")
        if info.st_size != manifest.pack_bytes:
            raise StageError("the seed pack's size is not the manifest's")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(_CHUNK), b""):
            digest.update(chunk)
    if digest.hexdigest() != manifest.pack_sha256:
        raise StageError("the seed pack's SHA-256 is not the manifest's")


def _check_bind_manifest(request: Request, paths: Paths) -> None:
    """In the bind form, ``/source``'s manifest is the one the request carries."""
    path = os.path.join(paths.source, STAGED_SEED, SEED_MANIFEST)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise StageError(f"/source's seed manifest cannot be opened ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        raw = handle.read(MAX_REQUEST_BYTES + 1)
    if parse_manifest(raw, "sha256:" + hashlib.sha256(raw).hexdigest()) != request.manifest:
        raise StageError("/source's seed manifest is not the one the controller handed over")


def _empty_workspace(paths: Paths) -> None:
    try:
        info = os.lstat(paths.workspace)
    except OSError as exc:
        raise StageError(f"/workspace cannot be read ({exc.strerror})") from None
    if not stat.S_ISDIR(info.st_mode):
        raise StageError("/workspace is not a directory")
    if os.listdir(paths.workspace):
        raise StageError("/workspace is not empty before staging; a volume is never reused")


def _write(path: str, data: bytes, mode: int = 0o644) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC
    fd = os.open(path, flags, mode)
    with os.fdopen(fd, "wb") as out:
        out.write(data)


def _index(git: Git, git_dir: str, cwd: str, pack: str, manifest: Manifest) -> None:
    """``.git/shallow`` naming the boundaries, then ``index-pack --strict`` of the pack."""
    if manifest.boundaries:
        lines = "".join(f"{oid}\n" for oid in sorted(set(manifest.boundaries)))
        _write(os.path.join(git_dir, "shallow"), lines.encode("ascii"))
    with open(pack, "rb") as stream:
        git.run(["index-pack", "--strict", "--stdin"], cwd=cwd, git_dir=git_dir, stdin=stream)


def _checkout(git: Git, git_dir: str, work_tree: str, commit: str) -> None:
    """The commit's tree into ``work_tree``, every conversion unset; then the override removed."""
    info = os.path.join(git_dir, "info")
    os.makedirs(info, mode=0o755, exist_ok=True)
    attributes = os.path.join(info, "attributes")
    _write(attributes, CHECKOUT_ATTRIBUTES)
    try:
        git.run(
            ["read-tree", "--reset", "-u", commit],
            cwd=work_tree,
            git_dir=git_dir,
            work_tree=work_tree,
        )
    finally:
        os.unlink(attributes)


def stage_agent(request: Request, paths: Paths, git: Git) -> dict[str, object]:
    """An agent's ``/workspace``: the seeded repository, ``HEAD`` detached, the tree checked out."""
    manifest = request.manifest
    pack = _pack_path(request, paths)
    _check_pack(pack, manifest)
    workspace = paths.workspace
    git_dir = os.path.join(workspace, ".git")
    init = ["init", "--quiet", "--template="]
    if request.object_format == "sha256":
        init.append("--object-format=sha256")
    git.run([*init, workspace], cwd=workspace)
    _write(os.path.join(git_dir, "config"), config_text(request.object_format).encode("ascii"))
    _index(git, git_dir, workspace, pack, manifest)
    for ref, oid in manifest.tags:
        git.run(["update-ref", "--no-deref", ref, oid], cwd=workspace, git_dir=git_dir)
    git.run(["update-ref", "--no-deref", "HEAD", manifest.head], cwd=workspace, git_dir=git_dir)
    _checkout(git, git_dir, workspace, manifest.head)
    return {"stager": "staged", "role": "agent", "head": manifest.head, "tags": len(manifest.tags)}


def _copy_tree(source: str, target: str) -> int:
    """``source`` (the host-proved tree) copied into ``target``: files, links, directories only."""
    copied = 0
    for name in sorted(os.listdir(source)):
        src = os.path.join(source, name)
        dst = os.path.join(target, name)
        info = os.lstat(src)
        if stat.S_ISLNK(info.st_mode):
            os.symlink(os.readlink(src), dst)
        elif stat.S_ISDIR(info.st_mode):
            os.mkdir(dst, 0o755)
            copied += _copy_tree(src, dst)
        elif stat.S_ISREG(info.st_mode):
            mode = 0o755 if info.st_mode & stat.S_IXUSR else 0o644
            flags = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC
            with os.fdopen(os.open(src, flags), "rb") as reader:
                fd = os.open(dst, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
                with os.fdopen(fd, "wb") as writer:
                    shutil.copyfileobj(reader, writer, _CHUNK)
            os.chmod(dst, mode)
        else:
            raise StageError(
                f"/source's tree holds something not a file, link or directory: {name}"
            )
        copied += 1
    return copied


def stage_export(request: Request, paths: Paths, git: Git) -> dict[str, object]:
    """An export command's ``/workspace``: the commit's tree, no ``.git``, in either form."""
    manifest = request.manifest
    if request.form == "bind":
        tree = os.path.join(paths.source, STAGED_TREE)
        if not os.path.isdir(tree) or os.path.islink(tree):
            raise StageError("/source holds no staged tree")
        entries = _copy_tree(tree, paths.workspace)
        return {"stager": "staged", "role": "export", "form": "bind", "entries": entries}
    pack = _pack_path(request, paths)
    _check_pack(pack, manifest)
    private = tempfile.mkdtemp(prefix="autoforge-stage-git-", dir=paths.tmp)
    try:
        init = ["init", "--quiet", "--bare", "--template="]
        if request.object_format == "sha256":
            init.append("--object-format=sha256")
        git.run([*init, private], cwd=private)
        _index(git, private, private, pack, manifest)
        _checkout(git, private, paths.workspace, manifest.head)
    finally:
        shutil.rmtree(private, ignore_errors=True)
    return {"stager": "staged", "role": "export", "form": "copy", "head": manifest.head}


def stage(request: Request, paths: Paths = DEFAULT_PATHS) -> dict[str, object]:
    """The ``stage`` command: ``/workspace`` written for ``request``'s role and form."""
    _empty_workspace(paths)
    if request.form == "bind":
        _check_bind_manifest(request, paths)
    home = tempfile.mkdtemp(prefix="autoforge-stager-home-", dir=paths.tmp)
    try:
        git = Git(home)
        if request.role == "agent":
            return stage_agent(request, paths, git)
        return stage_export(request, paths, git)
    finally:
        shutil.rmtree(home, ignore_errors=True)


def main(argv: list[str], stdin: BinaryIO, stdout: BinaryIO, paths: Paths = DEFAULT_PATHS) -> int:
    """``receive`` or ``stage``; one JSON verdict line on ``stdout``; 0 only on success."""
    try:
        if argv == ["receive"]:
            verdict = receive(stdin, paths)
        elif argv == ["stage"]:
            verdict = stage(parse_request(stdin.read(MAX_REQUEST_BYTES + 1)), paths)
        else:
            raise StageError("usage: container_stager.py receive|stage (the request on stdin)")
        code = 0
    except StageError as exc:
        verdict, code = {"stager": "failed", "problem": str(exc)}, 1
    except OSError as exc:
        verdict = {"stager": "failed", "problem": f"{type(exc).__name__}: {exc.strerror}"}
        code = 1
    stdout.write((json.dumps(verdict, sort_keys=True) + "\n").encode("utf-8"))
    stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer))
