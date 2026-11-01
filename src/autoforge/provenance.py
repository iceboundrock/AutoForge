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
writes. Every path is shown as the process has it: redacted whole, then
escaped when it holds a character that is not printable, so a path cannot
forge a line of the diagnostic, and only then bounded, so a cut never
splits a credential the redaction would have recognised (:func:`shown`).
The state file's own facts (its label, run id and writer) arrive already
shown this way, and shape-checked, on the error.

:func:`refusal_headline` and :func:`mismatch_lines` render the operator
diagnostic the CLI prints on stderr for ``status``, ``step``, ``resume``,
``unblock`` and ``run``. It names a recovery command only with what is
verified (the flags this command was given and its working directory),
each flag value as typed only when it can be shown faithfully and safely,
else a labelled placeholder, and a labelled placeholder for the controller
to use. It never suggests editing the state, ``run --force``, a downgrade,
a checkout or an installation.

:func:`refusal_report` is the same refusal as one line of a scheduler
report (``epic run``, ``epic resume``, ``epic run --dry-run`` and ``epic
status``, human and JSON): the refusal and the controller that refused it
first, the state file last, each part bounded on its own, so no path
crowds the controller's provenance out of the line (#265 R3-F1).
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
# on Linux, and no operator reads more of one in a diagnostic line. A flag
# value longer than this is not repeated in a recovery command at all.
MAX_PATH_CHARS = 1024
# A protocol refusal as a scheduler report keeps it whole
# (:func:`refusal_report`): the loader's headline (its label cut to 64
# characters), the refusing controller's version and protocol, its import
# path and its interpreter, and the state file, each path bounded on its own
# to MAX_PATH_CHARS. Those parts are at most about 3,400 characters together
# (tests/test_protocol_compat.py pins it), so a report is never cut here.
MAX_REFUSAL_CHARS = 4 * MAX_PATH_CHARS
_CUT = " [... cut]"
UNKNOWN = "(unknown)"


def shown(text: str, limit: int) -> str:
    """``text`` as one printable line of at most ``limit`` characters, redacted first.

    The order is the point (#265 R1-F2): the whole value is redacted
    before anything else, so a credential is recognised as the redaction
    recognises it anywhere else; a value that still holds a character that
    is not printable (a newline, an escape sequence, a bidirectional
    override) is then shown as its ``repr``, one visibly odd line; and only
    that is cut, with the cut said. Cutting first would leave a credential
    that crosses the cut unrecognised in what is shown, its prefix printed.
    """
    text = redact(text)
    if not text.isprintable():
        text = repr(text)
    if len(text) > limit:
        text = text[: limit - len(_CUT)] + _CUT
    return text


def shown_path(path: object) -> str:
    """``path`` as one bounded, printable, redacted string (:func:`shown`), or ``(unknown)``.

    A path is a fact of this host, not a secret, but it is redacted like
    everything the CLI prints, before it is escaped and before any path
    longer than :data:`MAX_PATH_CHARS` is cut.
    """
    if not isinstance(path, str) or not path:
        return UNKNOWN
    return shown(path, MAX_PATH_CHARS)


# The options whose values a recovery command repeats (``cli._global_flags``).
_VALUED_OPTIONS = ("--config", "--state-dir")


def _faithful(word: object) -> bool:
    """Whether ``word`` can be repeated in a command exactly as typed, and safely.

    Printable (no newline, escape or invisible formatting character), at
    most :data:`MAX_PATH_CHARS` long, and nothing the redaction would
    change: a value that fails any of these is not shown at all, since
    shell quoting keeps a word one argument but does not make it safe or
    short to print, and a redacted or cut one would not be the value typed.
    """
    return (
        isinstance(word, str)
        and word.isprintable()
        and len(word) <= MAX_PATH_CHARS
        and redact(word) == word
    )


def command_words(flags: Sequence[str]) -> tuple[list[str], bool]:
    """``flags`` as shell words for a recovery command, and whether any is a placeholder.

    A word that can be shown faithfully (:func:`_faithful`) is shell-quoted;
    any other is a labelled placeholder naming what it stands for (``<the
    --state-dir value this command was given>``), so the line is still a
    command once the operator puts the value back, and nothing of the value
    is printed.
    """
    words: list[str] = []
    placeholder = False
    previous = ""
    for word in flags:
        if _faithful(word):
            words.append(shlex.quote(word))
        else:
            placeholder = True
            if previous in _VALUED_OPTIONS:
                words.append(f"<the {previous} value this command was given>")
            else:
                words.append("<an argument this command was given>")
        previous = word if isinstance(word, str) else ""
    return words, placeholder


def refusal_headline(exc: StateProtocolError) -> str:
    """The diagnostic's first line: the loader's refusal, naming the state file shown safely."""
    where = f"state file {shown_path(exc.path)}: " if exc.path else ""
    return redact(f"autoforge: error: {where}{exc.headline}")


def refusal_report(exc: StateProtocolError, *, name_state: bool = True) -> str:
    """``exc`` as one line a scheduler report keeps whole: what was refused and by whom first.

    The refusal's reason (the persisted label and this controller's
    protocol, then the refusing controller's version, import path and
    interpreter: :meth:`ControllerIdentity.summary`), and then, with
    ``name_state``, the state file it refused (#265 R3-F1). Every part was
    shown on its own budget (:func:`shown_path`, the label's 64
    characters) before it was put in the line, so a long state path never
    crowds out the provenance: the state file comes last and has a
    :data:`MAX_PATH_CHARS` budget of its own, and the whole is under
    :data:`MAX_REFUSAL_CHARS`, which every scheduler report of it keeps
    (the bound here only guards that by construction). A report that
    already names the state file (the standalone state's, by
    :attr:`~autoforge.ownership.StandaloneHold.shown_state_file`) leaves
    ``name_state`` false.
    """
    text = exc.reason
    if name_state and exc.path:
        text += f"; state file {shown_path(exc.path)}"
    return shown(text, MAX_REFUSAL_CHARS)


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
    The recovery command is printed with those verified parts, each as
    typed only when it can be shown faithfully (:func:`command_words`),
    and a labelled placeholder for the controller to run it with; this
    module never knows where a compatible controller is. Every line is
    redacted, one line, and bounded.

    The ``result`` line says only what holds wherever the refusal was
    raised (#265 R1-F5): before the lock (the preflight of ``step``,
    ``resume``, ``unblock`` and ``run``, ``status`` and every dry run),
    where nothing at all was written, and under it (a newer state that
    replaced the file after the preflight), where taking the lock may
    already have created or rewritten the lock's PID record. In both the
    state file and its run logs were not rewritten, the agent worktree was
    not touched, no agent was launched and nothing was written to GitHub.
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
        "  result:     refused: the state file and its run logs were not rewritten, the "
        "agent worktree was not touched, no agent was launched and nothing was written to "
        "GitHub",
    ]
    if exc.newer:
        placeholder = f"<an autoforge that speaks protocol {exc.newer}>"
        words, held_back = command_words(flags)
        command = " ".join([placeholder, *words, "status"])
        lines += [
            "  next:       a newer controller wrote this run. With one that speaks protocol "
            f"{exc.newer}, from the cwd above and with this command's --config and "
            "--state-dir, start with:",
            f"                {command}",
        ]
        if held_back:
            lines.append(
                "              A <the ... value this command was given> stands for a value "
                "not repeated here, as it is not printable, is longer than "
                f"{MAX_PATH_CHARS} characters or is shaped like a credential: put back the "
                "value you typed."
            )
        lines += [
            "              What an interpreter imports: <its python> -c 'import autoforge "
            "as a; print(a.__protocol_version__, a.__file__)'",
            "              Leave the state file as it is: no edit of it, and no older "
            "controller, continues this run.",
        ]
    else:
        lines += [
            "  next:       no AutoForge protocol has this label, so no controller reads the "
            "file as it is and this one cannot tell which wrote it. Check that the import "
            "and python above are the controller you meant and that the state above is the "
            "run's file (this cwd, --config and --state-dir). Leave the state file as it "
            "is: neither an edit nor setting it aside continues the run. See 'A run a newer "
            "controller wrote' in docs/usage.md.",
        ]
    # The run id and the flags are the file's and the operator's: each line
    # crosses the redaction boundary here, whoever prints it. Every part was
    # shown (redacted, one line and bounded) before it was put in a line.
    return [redact(line) for line in lines]
