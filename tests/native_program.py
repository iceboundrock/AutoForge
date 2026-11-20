"""A native program that runs a script, for the tests of a launch bound to its program.

A launch that needs a fresh context refuses an executable that is a script
(``#!``): the kernel hands a script to its interpreter by a path, and an
interpreter may open it again by its name, as Node does, so no descriptor binds
what runs (R6-F3 of PR #513). Its tests therefore need a native program that the
kernel loads from the file itself. :func:`write_native_program` writes one: a
small C program, compiled once per test process with the host's C compiler,
whose copy at ``path`` ``exec`` s the script named when the copy was written,
with the arguments it was given. The script's path is written into the copy's
data, so two copies at one name differ as two builds do, and a copy mounted
elsewhere (inside the sandbox) still runs its own script.

Linux only: a patched Mach-O loses its signature, and macOS cannot start a
program from its descriptor anyway. A host without a C compiler skips the tests
that need one (:data:`needs_native_program`).
"""

from __future__ import annotations

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

#: Where the script's path is written in a copy: the bytes of a data array.
_SLOT = 1024
_PLACEHOLDER = b"AUTOFORGE-NATIVE-PROGRAM-SCRIPT"

_SOURCE = f"""
#include <stdio.h>
#include <unistd.h>

static char script[{_SLOT}] = "{_PLACEHOLDER.decode()}";

int main(int argc, char **argv) {{
    char *args[argc + 1];
    args[0] = script;
    for (int i = 1; i < argc; i++) args[i] = argv[i];
    args[argc] = NULL;
    execv(script, args);
    perror("execv");
    return 127;
}}
"""


@functools.cache
def _compiled() -> bytes | str:
    """The compiled program's bytes, or why there are none (a ``str``)."""
    if not sys.platform.startswith("linux"):
        return f"a native stand-in is built on Linux only, not {sys.platform}"
    compiler = next((c for c in ("cc", "gcc", "clang") if shutil.which(c)), None)
    if compiler is None:
        return "no C compiler (cc, gcc or clang) to build a native stand-in"
    with tempfile.TemporaryDirectory() as scratch:
        source = Path(scratch) / "program.c"
        source.write_text(_SOURCE)
        out = Path(scratch) / "program"
        try:
            done = subprocess.run(  # noqa: S603 - argv list, no shell
                [compiler, "-O0", "-o", str(out), str(source)],
                capture_output=True,
                timeout=120,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"{compiler} could not build a native stand-in ({exc})"
        if done.returncode != 0 or not out.is_file():
            return f"{compiler} could not build a native stand-in (exit {done.returncode})"
        data = out.read_bytes()
    if data.count(_PLACEHOLDER) != 1:
        return "the native stand-in's script slot was not found once in its build"
    return data


def native_program_problem() -> str:
    """Why this host cannot build a native stand-in; ``""`` when it can."""
    built = _compiled()
    return built if isinstance(built, str) else ""


needs_native_program = pytest.mark.skipif(
    bool(native_program_problem()), reason=native_program_problem() or "a native stand-in"
)


def write_native_program(path: Path, script: Path) -> Path:
    """A native program at ``path`` that ``exec`` s ``script`` (absolute) with its arguments.

    Written beside its name and renamed over it, as an installer puts a build
    in place, so a program another test holds open keeps its own bytes.
    """
    built = _compiled()
    if isinstance(built, str):
        pytest.skip(built)
    target = os.fsencode(os.path.abspath(script))
    if len(target) >= _SLOT:
        raise ValueError(f"{script} is too long for the native stand-in's slot")
    at = built.index(_PLACEHOLDER)
    data = built[:at] + target + b"\0" * (_SLOT - len(target)) + built[at + _SLOT :]
    staging = path.with_name(path.name + ".native")
    staging.write_bytes(data)
    staging.chmod(0o755)
    os.replace(staging, path)
    return path


def write_script(path: Path, text: str) -> Path:
    """An executable script at ``path``: what a native stand-in runs."""
    path.write_text(text)
    path.chmod(0o755)
    return path
