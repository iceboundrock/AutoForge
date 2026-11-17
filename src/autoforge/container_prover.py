"""The prover: proves a container launch's ``/workspace`` before any agent or command runs on it.

ADR 0013 D2 (required changes B and D) and D13's stage step (#271). It runs
in the launch's stager container, offline and with no credential, as its
own exec after the stager. Its verdict is one JSON line on standard
output; the controller reads it from the exec's output and a mismatch is a
setup failure (D17), never a result.

It imports nothing of :mod:`autoforge` and only the standard library, so
the image carries this file alone and runs it with ``python3 -I``. Its
request, one JSON object on standard input (:func:`parse_request`), comes
from the controller: the expected tree id read from the shared store and,
for an agent, the expected ref set and shallow boundaries from the manifest
the controller built, whose digest the launch's git input binds. Nothing
the prover expects is read from the pack, the manifest copy in the
container or the stager's verdict, so a manifest changed inside the
container changes nothing.

**The tree proof**, in both forms and for both roles: ``/workspace``
(its ``.git`` aside, for an agent) against the expected tree. The tree
objects come from the seed pack, indexed into a private git directory of
the prover's own, never the stager's repository; each one's content is
re-hashed and must hash to the id it was asked for, so neither git nor the
pack can substitute one. Every file on disk is hashed as a blob from its
bytes, every link's target too; a mode, a missing or extra entry, a
non-empty gitlink directory or a byte changed after the stager fails the
proof. In the copy-in form it is the only tree proof (D2 says why that is
acceptable).

**The seeded repository's checks**, for an agent, read as plain files from
``/workspace/.git`` (no git process runs with it as its repository):

- ``.git`` is a directory, not a link or a ``gitdir:`` file;
- the refs are exactly the expected set: ``HEAD`` detached at the expected
  commit, and exactly the expected tag refs at their object ids; no other
  ref, no ``packed-refs``, no symbolic ref;
- no reflog (no ``logs``);
- ``.git/shallow`` names exactly the expected boundaries;
- no alternates (``objects/info/alternates`` or ``http-alternates``);
- no hook (no ``hooks`` directory) and nothing in ``info``;
- the configuration holds exactly the stager's keys and values
  (:func:`expected_config`), so no remote, no ``core.hooksPath``, no
  ``include`` and no filter;
- nothing else at ``.git``'s top level.

For an export command, there is no ``.git`` at all.
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
from collections.abc import Iterator
from dataclasses import dataclass
from typing import BinaryIO

SOURCE = "/source"
WORKSPACE = "/workspace"
RECEIVED_PACK = "/tmp/autoforge-seed/seed.pack"
STAGED_SEED = "seed"
SEED_PACK = "seed.pack"

ROLES = ("agent", "export")
FORMS = ("bind", "copy")
MAX_REQUEST_BYTES = 8 * 1024 * 1024
MAX_PROBLEMS = 20
MAX_TREE_BYTES = 64 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 1800
_CHUNK = 1024 * 1024

_OID = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_RAW_LEN = {"sha1": 20, "sha256": 32}
_TAG_REF = re.compile(r"refs/tags/[A-Za-z0-9._/+@-]{1,200}")

# What ``.git``'s top level may hold after the stager, and nothing else.
GIT_ENTRIES = frozenset({"HEAD", "config", "index", "objects", "refs", "shallow", "info"})
_MODE_TREE = b"40000"
_MODE_FILE = b"100644"
_MODE_EXEC = b"100755"
_MODE_LINK = b"120000"
_MODE_GITLINK = b"160000"


class ProveError(Exception):
    """The prover could not run its checks at all: a setup failure, like a mismatch."""


def expected_config(object_format: str) -> tuple[tuple[str, str], ...]:
    """Every key and value the stager writes, as ``git config --list`` names them; nothing else."""
    version = "1" if object_format == "sha256" else "0"
    keys = [
        ("core.repositoryformatversion", version),
        ("core.filemode", "true"),
        ("core.bare", "false"),
        ("core.logallrefupdates", "false"),
    ]
    if object_format == "sha256":
        keys.append(("extensions.objectformat", "sha256"))
    return tuple(sorted(keys))


@dataclass(frozen=True)
class Paths:
    source: str = SOURCE
    workspace: str = WORKSPACE
    received_pack: str = RECEIVED_PACK
    tmp: str = "/tmp"


DEFAULT_PATHS = Paths()


@dataclass(frozen=True)
class Request:
    """What the controller expects: never read from the container.

    ``tree`` is the commit's tree id from the shared store; for an agent
    ``head`` the commit ``HEAD`` is detached at, ``tags`` the exact tag
    refs with their ids, ``shallow`` the exact boundaries.
    """

    role: str
    form: str
    object_format: str
    tree: str
    head: str = ""
    tags: tuple[tuple[str, str], ...] = ()
    shallow: tuple[str, ...] = ()


def _oid(value: object, object_format: str, what: str) -> str:
    pattern = _OID[object_format]
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ProveError(f"the request's {what} is not a full object id")
    return value


def parse_request(raw: bytes) -> Request:
    """The request: exact keys for its role, full object ids, plain tag refs."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise ProveError("the request is over its bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ProveError("the request does not parse") from None
    if not isinstance(data, dict):
        raise ProveError("the request is not an object")
    role, form, object_format = data.get("role"), data.get("form"), data.get("object_format")
    if role not in ROLES or form not in FORMS or object_format not in _OID:
        raise ProveError("the request names an unknown role, form or object format")
    common = {"role", "form", "object_format", "tree"}
    keys = common | ({"head", "tags", "shallow"} if role == "agent" else set())
    if set(data) != keys:
        raise ProveError("the request does not hold exactly its role's fields")
    tree = _oid(data["tree"], object_format, "tree")
    if role == "export":
        return Request(role, form, object_format, tree)
    head = _oid(data["head"], object_format, "head")
    if not isinstance(data["tags"], list) or not isinstance(data["shallow"], list):
        raise ProveError("the request's tags or shallow boundaries are not lists")
    tags: list[tuple[str, str]] = []
    for tag in data["tags"]:
        if not isinstance(tag, dict) or set(tag) != {"ref", "object"}:
            raise ProveError("a requested tag is not a ref and an object")
        ref = tag["ref"]
        if not isinstance(ref, str) or not _TAG_REF.fullmatch(ref) or ".." in ref:
            raise ProveError("a requested tag's ref is not a plain tag ref")
        tags.append((ref, _oid(tag["object"], object_format, "tag object")))
    shallow = tuple(_oid(b, object_format, "shallow boundary") for b in data["shallow"])
    return Request(role, form, object_format, tree, head, tuple(sorted(tags)), shallow)


# -- the objects --------------------------------------------------------------------


def _git_env(home: str, git_dir: str = "") -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": home,
        "XDG_CONFIG_HOME": home,
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_NO_LAZY_FETCH": "1",
        "GIT_OPTIONAL_LOCKS": "0",
    }
    if git_dir:
        env["GIT_DIR"] = git_dir
    return env


_GIT_OPTIONS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


class Objects:
    """The seed pack's objects in a private git directory; each object re-hashed on read."""

    def __init__(self, pack: str, object_format: str, tmp: str) -> None:
        self.object_format = object_format
        self._dir = tempfile.mkdtemp(prefix="autoforge-prove-", dir=tmp)
        self._home = os.path.join(self._dir, "home")
        self._git_dir = os.path.join(self._dir, "git")
        os.mkdir(self._home, 0o700)
        init = ["init", "--quiet", "--bare", "--template="]
        if object_format == "sha256":
            init.append("--object-format=sha256")
        self._run([*init, self._git_dir])
        try:
            fd = os.open(pack, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except OSError as exc:
            raise ProveError(f"the seed pack cannot be opened ({exc.strerror})") from None
        with os.fdopen(fd, "rb") as stream:
            # Not --strict: the prover needs objects, not connectivity, and
            # it re-hashes every object it reads.
            self._run(["index-pack", "--stdin"], stdin=stream)
        self._batch: subprocess.Popen[bytes] | None = None

    def _run(self, args: list[str], stdin: BinaryIO | None = None) -> bytes:
        try:
            proc = subprocess.run(
                ["git", *_GIT_OPTIONS, *args],
                cwd=self._dir,
                env=_git_env(self._home, self._git_dir),
                stdin=stdin if stdin is not None else subprocess.DEVNULL,
                capture_output=True,
                timeout=GIT_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise ProveError(f"git {args[0]} could not run ({type(exc).__name__})") from None
        if proc.returncode != 0:
            raise ProveError(f"git {args[0]} exited {proc.returncode}")
        return proc.stdout

    def close(self) -> None:
        if self._batch is not None:
            self._batch.kill()
            self._batch.wait()
            self._batch = None
        shutil.rmtree(self._dir, ignore_errors=True)

    def _cat(self, oid: str) -> tuple[bytes, bytes]:
        if self._batch is None:
            self._batch = subprocess.Popen(
                ["git", *_GIT_OPTIONS, "cat-file", "--batch"],
                cwd=self._dir,
                env=_git_env(self._home, self._git_dir),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        assert self._batch.stdin is not None and self._batch.stdout is not None
        self._batch.stdin.write(oid.encode("ascii") + b"\n")
        self._batch.stdin.flush()
        header = self._batch.stdout.readline().rstrip(b"\n").split(b" ")
        if len(header) != 3 or header[0].decode("ascii", "replace") != oid:
            raise ProveError(f"tree {oid} is not in the seed's objects")
        kind, size = header[1], int(header[2])
        if size > MAX_TREE_BYTES:
            raise ProveError(f"tree {oid} is over its bound")
        content = self._batch.stdout.read(size)
        self._batch.stdout.read(1)
        return kind, content

    def tree(self, oid: str) -> list[tuple[bytes, bytes, str]]:
        """Tree ``oid``'s entries ``(mode, name, id)``, its content proved to hash to ``oid``."""
        kind, content = self._cat(oid)
        if kind != b"tree" or object_id(b"tree", content, self.object_format) != oid:
            raise ProveError(f"the object read as tree {oid} does not hash to it")
        return parse_tree(content, self.object_format)


def object_id(kind: bytes, content: bytes, object_format: str) -> str:
    """The git object id of ``content`` as an object of ``kind``."""
    algo = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
    digest = algo(kind + b" " + str(len(content)).encode("ascii") + b"\0")
    digest.update(content)
    return digest.hexdigest()


def blob_id_of_file(fd: int, size: int, object_format: str) -> str:
    """The blob id of the open file ``fd``'s bytes, streamed."""
    algo = hashlib.sha256 if object_format == "sha256" else hashlib.sha1
    digest = algo(b"blob " + str(size).encode("ascii") + b"\0")
    read = 0
    while True:
        chunk = os.read(fd, _CHUNK)
        if not chunk:
            break
        read += len(chunk)
        digest.update(chunk)
    if read != size:
        raise ProveError("a file changed size while it was hashed")
    return digest.hexdigest()


def parse_tree(content: bytes, object_format: str) -> list[tuple[bytes, bytes, str]]:
    raw_len = _RAW_LEN[object_format]
    entries: list[tuple[bytes, bytes, str]] = []
    at = 0
    while at < len(content):
        space = content.index(b" ", at)
        nul = content.index(b"\0", space)
        mode, name = content[at:space], content[space + 1 : nul]
        oid = content[nul + 1 : nul + 1 + raw_len]
        if len(oid) != raw_len:
            raise ProveError("a tree entry is truncated")
        entries.append((mode, name, oid.hex()))
        at = nul + 1 + raw_len
    return entries


# -- the tree proof -----------------------------------------------------------------


def _describe(path: bytes) -> str:
    return os.fsdecode(path)


def prove_tree(objects: Objects, root: bytes, tree: str, *, skip_git: bool) -> Iterator[str]:
    """Every difference between the directory ``root`` and tree ``tree``, as text."""
    yield from _prove_dir(objects, root, b"", tree, skip_git=skip_git)


def _prove_dir(
    objects: Objects, root: bytes, rel: bytes, tree: str, *, skip_git: bool
) -> Iterator[str]:
    here = os.path.join(root, rel) if rel else root
    entries = objects.tree(tree)
    try:
        on_disk = set(os.listdir(here))
    except OSError as exc:
        yield f"{_describe(rel) or '/workspace'} cannot be listed ({exc.strerror})"
        return
    if skip_git:
        on_disk.discard(b".git")
    expected = {name for _, name, _ in entries}
    for extra in sorted(on_disk - expected):
        yield f"{_describe(os.path.join(rel, extra))} is not in the tree"
    for mode, name, oid in entries:
        path_rel = os.path.join(rel, name) if rel else name
        path = os.path.join(root, path_rel)
        shown = _describe(path_rel)
        if name not in on_disk:
            yield f"{shown} is missing"
            continue
        info = os.lstat(path)
        if mode == _MODE_TREE:
            if not stat.S_ISDIR(info.st_mode):
                yield f"{shown} is not a directory"
                continue
            yield from _prove_dir(objects, root, path_rel, oid, skip_git=False)
        elif mode == _MODE_GITLINK:
            if not stat.S_ISDIR(info.st_mode) or os.listdir(path):
                yield f"{shown} is a submodule, and is not an empty directory"
        elif mode == _MODE_LINK:
            if not stat.S_ISLNK(info.st_mode):
                yield f"{shown} is not a symbolic link"
                continue
            target = os.readlink(path)
            if object_id(b"blob", target, objects.object_format) != oid:
                yield f"{shown}'s target is not the tree's"
        elif mode in (_MODE_FILE, _MODE_EXEC):
            if not stat.S_ISREG(info.st_mode):
                yield f"{shown} is not a regular file"
                continue
            if bool(info.st_mode & stat.S_IXUSR) != (mode == _MODE_EXEC):
                yield f"{shown}'s executable bit is not the tree's"
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
            try:
                if blob_id_of_file(fd, info.st_size, objects.object_format) != oid:
                    yield f"{shown}'s bytes are not its blob's"
            finally:
                os.close(fd)
        else:
            yield f"{shown} has a mode no tree entry has ({mode.decode('ascii', 'replace')})"


# -- the seeded repository's checks ------------------------------------------------


def _read_small(path: str, limit: int = 1024 * 1024) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ProveError(f"{path} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ProveError(f"{path} is over its bound")
    return data


def _refs(git_dir: str) -> Iterator[tuple[str, str]]:
    """Every loose ref under ``refs``: its name and its raw content."""
    top = os.path.join(git_dir, "refs")
    for dirpath, dirnames, filenames in os.walk(top):
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(dirpath, name)
            ref = os.path.relpath(path, git_dir).replace(os.sep, "/")
            if os.path.islink(path):
                yield ref, "<a symbolic link>"
                continue
            yield ref, _read_small(path).decode("utf-8", "replace")


def _config(git_dir: str, home: str) -> list[tuple[str, str]]:
    """``.git/config``'s keys and values, read by git from that file alone, with no include."""
    config = os.path.join(git_dir, "config")
    if os.path.islink(config):
        raise ProveError("the seeded repository's configuration is a symbolic link")
    try:
        proc = subprocess.run(
            ["git", "config", "--file", config, "--no-includes", "--list", "-z"],
            cwd=home,
            env=_git_env(home),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProveError(f"the configuration could not be read ({type(exc).__name__})") from None
    if proc.returncode != 0:
        raise ProveError("the seeded repository's configuration does not parse")
    items = []
    for record in proc.stdout.split(b"\0"):
        if not record:
            continue
        key, _, value = record.partition(b"\n")
        items.append((key.decode("utf-8", "replace"), value.decode("utf-8", "replace")))
    return sorted(items)


def check_repository(request: Request, workspace: str, tmp: str) -> Iterator[str]:
    """Every way ``/workspace/.git`` differs from the seed's repository, as text (D2)."""
    git_dir = os.path.join(workspace, ".git")
    try:
        info = os.lstat(git_dir)
    except OSError:
        yield "there is no .git"
        return
    if not stat.S_ISDIR(info.st_mode):
        yield ".git is not a directory (a link or a gitdir file)"
        return
    names = set(os.listdir(git_dir))
    if "logs" in names:
        yield "a reflog: .git/logs exists"
    if "hooks" in names:
        yield "a hook: .git/hooks exists"
    if "packed-refs" in names:
        yield "an extra ref: .git/packed-refs exists"
    for name in sorted(names - GIT_ENTRIES - {"logs", "hooks", "packed-refs"}):
        yield f"an unexpected entry: .git/{name}"
    if "info" in names and os.listdir(os.path.join(git_dir, "info")):
        yield "an unexpected file in .git/info (attributes, excludes or grafts)"
    for name in ("alternates", "http-alternates"):
        if os.path.lexists(os.path.join(git_dir, "objects", "info", name)):
            yield f"an alternates file: .git/objects/info/{name}"
    # HEAD, detached at the expected commit.
    try:
        head = _read_small(os.path.join(git_dir, "HEAD")).decode("utf-8", "replace")
    except (OSError, ProveError):
        yield "HEAD cannot be read"
    else:
        if head != request.head + "\n":
            yield "HEAD is not detached at the expected commit"
    # Exactly the expected tag refs, and no other ref.
    found = dict(_refs(git_dir))
    wanted = dict(request.tags)
    for ref in sorted(set(found) - set(wanted)):
        yield f"an extra ref: {ref}"
    for ref in sorted(set(wanted) - set(found)):
        yield f"a missing ref: {ref}"
    for ref in sorted(set(found) & set(wanted)):
        if found[ref] != wanted[ref] + "\n":
            yield f"a moved ref: {ref} is not at the expected object"
    # The shallow boundaries.
    shallow_path = os.path.join(git_dir, "shallow")
    if os.path.lexists(shallow_path):
        try:
            lines = _read_small(shallow_path).decode("ascii", "replace").splitlines()
        except (OSError, ProveError):
            lines = ["<unreadable>"]
    else:
        lines = []
    if len(lines) != len(set(lines)) or set(lines) != set(request.shallow):
        yield ".git/shallow does not name exactly the manifest's boundaries"
    # The configuration: exactly the stager's keys.
    home = tempfile.mkdtemp(prefix="autoforge-prove-home-", dir=tmp)
    try:
        try:
            config = _config(git_dir, home)
        except ProveError as exc:
            yield str(exc)
            return
    finally:
        shutil.rmtree(home, ignore_errors=True)
    expected = list(expected_config(request.object_format))
    for key, _ in config:
        if key.startswith("remote."):
            yield f"a remote: the configuration names {key}"
        elif key == "core.hookspath":
            yield "a hook: the configuration names core.hookspath"
    if config != expected:
        unknown = sorted({key for key, _ in config} - {key for key, _ in expected})
        yield "the configuration holds other keys than the stager's" + (
            f" ({', '.join(unknown[:5])})" if unknown else " (a value differs)"
        )


# -- the command ----------------------------------------------------------------------


def _pack(request: Request, paths: Paths) -> str:
    if request.form == "bind":
        return os.path.join(paths.source, STAGED_SEED, SEED_PACK)
    return paths.received_pack


def prove(request: Request, paths: Paths = DEFAULT_PATHS) -> list[str]:
    """Every problem with ``/workspace`` for ``request`` (at most :data:`MAX_PROBLEMS`)."""
    problems: list[str] = []
    if request.role == "agent":
        problems.extend(check_repository(request, paths.workspace, paths.tmp))
    elif os.path.lexists(os.path.join(paths.workspace, ".git")):
        problems.append("an export command's workspace holds a .git")
    objects = Objects(_pack(request, paths), request.object_format, paths.tmp)
    try:
        for problem in prove_tree(
            objects, os.fsencode(paths.workspace), request.tree, skip_git=request.role == "agent"
        ):
            problems.append(problem)
            if len(problems) >= MAX_PROBLEMS:
                break
    finally:
        objects.close()
    return problems[:MAX_PROBLEMS]


def main(argv: list[str], stdin: BinaryIO, stdout: BinaryIO, paths: Paths = DEFAULT_PATHS) -> int:
    """``prove``: one JSON verdict line on ``stdout``; 0 only when ``/workspace`` is proved."""
    try:
        if argv != ["prove"]:
            raise ProveError("usage: container_prover.py prove (the request on stdin)")
        request = parse_request(stdin.read(MAX_REQUEST_BYTES + 1))
        problems = prove(request, paths)
        verdict: dict[str, object] = (
            {"prover": "proved", "tree": request.tree}
            if not problems
            else {"prover": "failed", "problems": problems}
        )
        code = 0 if not problems else 1
    except (ProveError, OSError, ValueError) as exc:
        verdict, code = {"prover": "failed", "problems": [f"{type(exc).__name__}: {exc}"]}, 1
    stdout.write((json.dumps(verdict, sort_keys=True) + "\n").encode("utf-8"))
    stdout.flush()
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer))
