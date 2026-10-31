"""Which controller is running, and what a protocol refusal tells the operator (#265).

A state file written by a newer controller is refused by
:meth:`autoforge.state.AutoForgeState.from_dict`
(:class:`~autoforge.errors.StateProtocolError`). The refusal is correct;
what the operator needs next is which controller refused it. With several
editable installs, ``uv`` projects and agent worktrees on one machine, the
code a command imports need not be the checkout the operator thinks they
selected, so this module reports only what the running process itself
knows:

- the ``autoforge`` package object actually imported, by its ``__file__``
  (never package metadata, a branch name or a ``uv`` project);
- ``sys.executable``, the interpreter running it;
- the version, protocol and prompt version that package declares.

Nothing here reads a file, the environment, git or GitHub, and nothing
writes. Every path is shown as the process has it, bounded, and escaped
when it holds a character that is not printable, so a path cannot forge a
line of the diagnostic. The state file's own facts (its label, run id and
writer) arrive already bounded and shape-checked on the error.

:func:`mismatch_lines` renders the operator diagnostic the CLI prints on
stderr for ``status``, ``step``, ``resume``, ``unblock`` and ``run``. It
names a recovery command only with what is verified (the flags this
command was given and its working directory) and a labelled placeholder
for the controller to use, and never suggests editing the state, ``run
--force``, a downgrade, a checkout or an installation.
"""

from __future__ import annotations

import os
import shlex
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from . import __prompt_version__, __protocol_version__, __version__
from .errors import StateProtocolError
from .redaction import redact

# A path longer than this is shown cut, with the cut said: PATH_MAX is 4096
# on Linux, and no operator reads more of one in a diagnostic line.
MAX_PATH_CHARS = 1024
_CUT = " [... cut]"
UNKNOWN = "(unknown)"


def shown_path(path: object) -> str:
    """``path`` as one bounded, printable string, or ``(unknown)`` when there is none.

    A path holding a character that is not printable (a newline, an escape
    sequence, a bidirectional override) is shown as its ``repr``, so it
    stays one line and visibly odd; any path longer than
    :data:`MAX_PATH_CHARS` is cut. A path is a fact of this host, not a
    secret, but it is still redacted like everything the CLI prints.
    """
    if not isinstance(path, str) or not path:
        return UNKNOWN
    text = path if path.isprintable() else repr(path)
    if len(text) > MAX_PATH_CHARS:
        text = text[: MAX_PATH_CHARS - len(_CUT)] + _CUT
    return redact(text)


@dataclass(frozen=True)
class ControllerIdentity:
    """The running controller, as the process knows it.

    ``module`` is the absolute path of the imported package's
    ``__init__.py``, ``""`` when the package has none; ``python`` is
    ``sys.executable``, ``""`` when Python does not know it.
    """

    version: str
    protocol: str
    prompt_version: str
    module: str
    python: str

    @classmethod
    def current(cls) -> ControllerIdentity:
        package = sys.modules[__package__ or "autoforge"]
        module = getattr(package, "__file__", None) or ""
        return cls(
            version=__version__,
            protocol=__protocol_version__,
            prompt_version=__prompt_version__,
            module=os.path.abspath(module) if module else "",
            python=sys.executable or "",
        )

    def summary(self) -> str:
        """One line: version, protocol, import path and interpreter."""
        return (
            f"autoforge {self.version} (protocol {self.protocol}) imported from "
            f"{shown_path(self.module)}, python {shown_path(self.python)}"
        )


def protocol_ranges(labels: Iterable[str]) -> str:
    """Decimal protocol labels as ranges, ascending: ``1-5 and 7-13``."""
    runs: list[list[int]] = []
    for n in sorted({int(label) for label in labels}):
        if runs and n == runs[-1][-1] + 1:
            runs[-1].append(n)
        else:
            runs.append([n])
    spans = [f"{run[0]}" if len(run) == 1 else f"{run[0]}-{run[-1]}" for run in runs]
    if len(spans) <= 1:
        return "".join(spans)
    return ", ".join(spans[:-1]) + " and " + spans[-1]


def mismatch_lines(
    exc: StateProtocolError,
    *,
    identity: ControllerIdentity,
    reads: str,
    cwd: str,
    flags: Sequence[str],
) -> list[str]:
    """The operator diagnostic for ``exc``: what was refused, by which controller, what next.

    ``reads`` is the protocols this controller loads
    (:func:`protocol_ranges`), ``cwd`` the command's working directory and
    ``flags`` the ``--config`` and ``--state-dir`` it was given, as typed.
    The recovery command is printed with those verified parts and a
    labelled placeholder for the controller to run it with; this module
    never knows where a compatible controller is. Every line is redacted.
    """
    recorded = []
    if exc.run_id:
        recorded.append(f"run {exc.run_id}")
    if exc.written_by:
        recorded.append(f"written by autoforge {exc.written_by}")
    lines = [
        "AutoForge controller/state protocol mismatch",
        f"  state:      {shown_path(exc.path)} (protocol_version {exc.label})",
    ]
    if recorded:
        lines.append(f"  recorded:   {', '.join(recorded)} (as the file says; it was not loaded)")
    lines += [
        f"  controller: autoforge {identity.version}, protocol {identity.protocol} "
        f"(reads {reads}), prompt {identity.prompt_version}",
        f"  import:     {shown_path(identity.module)}",
        f"  python:     {shown_path(identity.python)}",
        f"  cwd:        {shown_path(cwd)}",
        "  result:     refused before anything was written: the state file, its run logs "
        "and the agent worktree are unchanged; no agent was launched and nothing was "
        "written to GitHub",
    ]
    if exc.newer:
        placeholder = f"<an autoforge that speaks protocol {exc.newer}>"
        command = " ".join([placeholder, *(shlex.quote(f) for f in flags), "status"])
        lines += [
            "  next:       a newer controller wrote this run. With one that speaks protocol "
            f"{exc.newer}, from the cwd above and with this command's --config and "
            "--state-dir, start with:",
            f"                {command}",
            "              What an interpreter imports: <its python> -c 'import autoforge "
            "as a; print(a.__protocol_version__, a.__file__)'",
            "              Leave the state file as it is: no edit of it, and no older "
            "controller, continues this run.",
        ]
    else:
        lines += [
            "  next:       no AutoForge protocol has this label, so no controller reads the "
            "file as it is: it was edited, or is not AutoForge's state. See 'The state file "
            "is unreadable' in docs/usage.md.",
        ]
    # The run id and the flags are the file's and the operator's: each line
    # crosses the redaction boundary here, whoever prints it.
    return [redact(line) for line in lines]
