"""The exporter: reads a container launch's candidate out of its ``/workspace`` volume (#271).

ADR 0013 D5 and D13's export step. It runs in the launch's exporter
container, created after the launch container was removed, offline
(``--network none``), with no credential, binding nothing, and with the
workspace volume mounted **read-only**. Nothing it writes leaves the
container but its standard output, which the controller reads as
untrusted data and imports strictly (:mod:`autoforge.candidate_artifact`).

It imports nothing of :mod:`autoforge` and only the standard library, so
the image carries this file alone and runs it with ``python3 -I``. Two
commands, each one exec:

- ``head``: the commit ``/workspace/.git/HEAD`` names, as one JSON line.
  ``HEAD`` is read as a file (detached, or a symbolic ref to a loose ref
  or a ``packed-refs`` line): no git process reads the repository's
  configuration to resolve it. That commit is a claim the import checks.
- ``pack``: its request (the object format, the candidate and the commits
  to leave out: the seed and the carried commit) on standard input; the
  pack of every object the candidate reaches and they do not, on standard
  output. git runs with a private git directory of the exporter's own in
  ``/tmp`` whose object store is the workspace's
  (``GIT_OBJECT_DIRECTORY``) and whose ``shallow`` file is a copy of the
  workspace's: the repository's configuration, hooks and attributes are
  never read, and no system or global configuration either.

Whatever the agent left in its repository (a configuration, an alternates
file, a rewritten ``HEAD``) can make the export fail or carry objects the
import refuses; it cannot make the controller accept a candidate the
strict import does not.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from typing import BinaryIO

WORKSPACE = "/workspace"
MAX_REQUEST_BYTES = 64 * 1024
MAX_REF_BYTES = 4096
MAX_PACKED_REFS_BYTES = 16 * 1024 * 1024
GIT_TIMEOUT_SECONDS = 1800
_CHUNK = 1024 * 1024
_OID = {"sha1": re.compile(r"[0-9a-f]{40}"), "sha256": re.compile(r"[0-9a-f]{64}")}
_REF = re.compile(r"refs/[A-Za-z0-9._/+@-]{1,250}")
_GIT_OPTIONS = ("-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false")


class ExportError(Exception):
    """The export failed: the launch's candidate is refused, never guessed."""


def _read(path: str, limit: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ExportError(f"{os.path.basename(path)} cannot be read ({exc.strerror})") from None
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise ExportError(f"{os.path.basename(path)} is not a regular file")
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ExportError(f"{os.path.basename(path)} is over its bound")
    return data


def resolve_head(workspace: str = WORKSPACE) -> str:
    """The commit id ``HEAD`` names, read as files: detached, a loose ref, or ``packed-refs``."""
    git_dir = os.path.join(workspace, ".git")
    if os.path.islink(git_dir) or not os.path.isdir(git_dir):
        raise ExportError("/workspace/.git is not a directory")
    head = _read(os.path.join(git_dir, "HEAD"), MAX_REF_BYTES).decode("utf-8", "replace").strip()
    if any(pattern.fullmatch(head) for pattern in _OID.values()):
        return head
    if not head.startswith("ref: "):
        raise ExportError("HEAD is neither a commit id nor a symbolic ref")
    ref = head[len("ref: ") :]
    if not _REF.fullmatch(ref) or ".." in ref:
        raise ExportError("HEAD names a ref that is not a plain ref")
    loose = os.path.join(git_dir, *ref.split("/"))
    if os.path.lexists(loose):
        value = _read(loose, MAX_REF_BYTES).decode("utf-8", "replace").strip()
        if any(pattern.fullmatch(value) for pattern in _OID.values()):
            return value
        raise ExportError(f"{ref} does not hold a commit id")
    packed = os.path.join(git_dir, "packed-refs")
    if os.path.lexists(packed):
        for line in _read(packed, MAX_PACKED_REFS_BYTES).decode("utf-8", "replace").splitlines():
            oid, _, name = line.partition(" ")
            if name == ref and any(pattern.fullmatch(oid) for pattern in _OID.values()):
                return oid
    raise ExportError(f"HEAD names {ref}, which does not exist")


def parse_request(raw: bytes) -> tuple[str, str, tuple[str, ...]]:
    """The ``pack`` request: the object format, the candidate, the commits left out."""
    if len(raw) > MAX_REQUEST_BYTES:
        raise ExportError("the request is over its bound")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        raise ExportError("the request does not parse") from None
    if not isinstance(data, dict) or set(data) != {"object_format", "candidate", "exclude"}:
        raise ExportError("the request does not hold exactly its fields")
    pattern = _OID.get(data["object_format"])
    if pattern is None:
        raise ExportError("the request names an unknown object format")
    candidate, exclude = data["candidate"], data["exclude"]
    if not isinstance(candidate, str) or not pattern.fullmatch(candidate):
        raise ExportError("the request's candidate is not a full object id")
    if not isinstance(exclude, list) or not all(
        isinstance(oid, str) and pattern.fullmatch(oid) for oid in exclude
    ):
        raise ExportError("the request's excluded commits are not full object ids")
    return data["object_format"], candidate, tuple(exclude)


def pack(
    object_format: str,
    candidate: str,
    exclude: tuple[str, ...],
    out: BinaryIO,
    *,
    workspace: str = WORKSPACE,
    tmp: str = "/tmp",
) -> None:
    """The candidate's pack above ``exclude``, written to ``out``."""
    objects = os.path.join(workspace, ".git", "objects")
    if os.path.islink(objects) or not os.path.isdir(objects):
        raise ExportError("/workspace/.git/objects is not a directory")
    private = tempfile.mkdtemp(prefix="autoforge-export-", dir=tmp)
    try:
        home = os.path.join(private, "home")
        git_dir = os.path.join(private, "git")
        os.mkdir(home, 0o700)
        env = {
            "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_NO_LAZY_FETCH": "1",
            "GIT_OPTIONAL_LOCKS": "0",
        }
        init = ["git", *_GIT_OPTIONS, "init", "--quiet", "--bare", "--template="]
        if object_format == "sha256":
            init.append("--object-format=sha256")
        proc = subprocess.run(
            [*init, git_dir], env=env, cwd=private, capture_output=True, check=False
        )
        if proc.returncode != 0:
            raise ExportError("the exporter's git directory could not be made")
        shallow = os.path.join(workspace, ".git", "shallow")
        if os.path.lexists(shallow):
            data = _read(shallow, MAX_PACKED_REFS_BYTES)
            with open(os.path.join(git_dir, "shallow"), "wb") as handle:
                handle.write(data)
        env["GIT_DIR"] = git_dir
        env["GIT_OBJECT_DIRECTORY"] = objects
        revs = "".join([f"{candidate}\n", *(f"^{oid}\n" for oid in exclude)]).encode("ascii")
        errors = os.path.join(private, "stderr")
        with open(errors, "wb") as err:
            try:
                packer = subprocess.Popen(
                    ["git", *_GIT_OPTIONS, "pack-objects", "--stdout", "--revs", "--quiet"],
                    env=env,
                    cwd=private,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=err,
                )
            except OSError as exc:
                raise ExportError(f"git pack-objects could not run ({exc.strerror})") from None
            stdin, stdout = packer.stdin, packer.stdout
            assert stdin is not None and stdout is not None
            try:
                stdin.write(revs)
                stdin.close()
                for chunk in iter(lambda: stdout.read(_CHUNK), b""):
                    out.write(chunk)
                code = packer.wait(timeout=GIT_TIMEOUT_SECONDS)
            except BaseException:
                packer.kill()
                packer.wait()
                raise
        if code != 0:
            with open(errors, "rb") as err_in:
                lines = err_in.read(65536).decode("utf-8", "replace").strip().splitlines()
            raise ExportError(
                f"git pack-objects exited {code}" + (f": {lines[-1][:300]}" if lines else "")
            )
    finally:
        shutil.rmtree(private, ignore_errors=True)


def main(argv: list[str], stdin: BinaryIO, stdout: BinaryIO, stderr: BinaryIO) -> int:
    """``head`` (a JSON line) or ``pack`` (the pack's bytes); a failure on standard error."""
    try:
        if argv == ["head"]:
            line = json.dumps({"exporter": "head", "candidate": resolve_head()}, sort_keys=True)
            stdout.write((line + "\n").encode("utf-8"))
        elif argv == ["pack"]:
            object_format, candidate, exclude = parse_request(stdin.read(MAX_REQUEST_BYTES + 1))
            pack(object_format, candidate, exclude, stdout)
        else:
            raise ExportError("usage: container_exporter.py head|pack")
        stdout.flush()
        return 0
    except (ExportError, OSError) as exc:
        stderr.write(
            (json.dumps({"exporter": "failed", "problem": str(exc)}) + "\n").encode("utf-8")
        )
        stderr.flush()
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer, sys.stderr.buffer))
