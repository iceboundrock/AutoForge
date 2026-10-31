"""The outer sandbox's mode a run is pinned to across its commands (#280).

Why this module exists
----------------------

Every command reads the configuration again, and ``sandbox.mode`` used to
be read at the step that used it: a run started under ``required`` resumed
under ``disabled`` with no notice (ADR 0005 D12.3 and D15.3 before #280).
Two things made that a hole rather than a convenience. A command run
without ``--config`` falls back to the built-in default, ``disabled``. And
a configuration file a sandboxed agent could rewrite let it switch the
sandbox off for every later launch, while the controller's own GitHub
command and ``doctor``'s agent checks ran on what the file said. The
placement rule (:func:`autoforge.sandbox_policy.config_file_problem`)
keeps such a file out of every tree a launch writes; this pin makes a
weaker mode, however it came about, a decision the operator takes in the
open.

The rule
--------

A run records the mode it is created under (``state.sandbox_pin``). Each
``step``, ``resume`` and ``unblock``, and a scheduler worker resuming an
issue, compares the mode it would run under with the pin before any phase
executes (:meth:`autoforge.engine.ControllerEngine.check_run_configuration`):

- **weaker:** refused, with nothing written, unless the operator passes
  ``--accept-config-change``; the change is then written to the run log
  first, and the run is pinned to the weaker mode (``accepted``);
- **stronger:** the run is pinned to it (``strengthened``) with the
  command's next save, so going back is a downgrade like any other;
- **as strong:** nothing changes.

Strength is what a launch of the run gets (:func:`mode_strength`):
``disabled`` is logical isolation only, LOCAL's ``preferred`` falls back to
it where the host cannot enforce, and ``required`` never runs a launch
unenforced, which is also what REMOTE's ``preferred`` does (ADR 0005
D12.2). In a REMOTE run ``required`` and ``preferred`` are therefore one
strength, and moving between them changes nothing.

The pin holds configuration, never enforcement. Whether a launch ran inside
the sandbox stays the run log's (ADR 0005 D16.1), and the host's capability
may change between commands without touching the pin: under ``required``
an incapable host is refused at the gate, as before.

It applies to LOCAL and REMOTE runs alike, beside LOCAL's run contract and
REMOTE's recorded configuration rather than inside either. The contract
refuses any drift and an older contract is never migrated; the recorded
configuration is REMOTE's alone and refuses any schema but its own. The pin
refuses only a weaker mode, and it must load in every run created before
it existed.

The record
----------

``{"schema": 1, "mode": ..., "source": ..., "recorded_at": ...}``, or
``{}`` for a run loaded from a state file written before the pin (protocol
11 or older): such a run is not checked, and its first ``step``, ``resume``
or ``unblock`` pins the mode it runs under (``upgraded``), saved with that
command's next save.

A later sandbox fact (the backend of a REMOTE run, #273) is added to this
record under a new schema whose parser still reads schema 1, the new fact
absent and pinned by the next command (``upgraded``), so that a run created
before it stays loadable. That is the rule a fact added here follows; a
schema the parser does not know is refused.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from .config import SANDBOX_MODES, SANDBOX_PREFERRED, SANDBOX_REQUIRED
from .errors import ConfigurationError, StateError

#: Schema of the persisted pin. A sandbox fact added later takes a new
#: schema whose parser also reads this one (see "The record" above).
PIN_SCHEMA = 1

#: How the pin came to be: ``created`` by ``run`` (or a scheduler's worker);
#: ``upgraded`` by the first command that found a state file written before
#: #280 without one; ``strengthened`` by a command under a stronger mode;
#: ``accepted`` by an operator's ``--accept-config-change`` under a weaker one.
SOURCE_CREATED = "created"
SOURCE_UPGRADED = "upgraded"
SOURCE_STRENGTHENED = "strengthened"
SOURCE_ACCEPTED = "accepted"
SOURCES = (SOURCE_CREATED, SOURCE_UPGRADED, SOURCE_STRENGTHENED, SOURCE_ACCEPTED)

#: What a refusal, a notice and the run log call the pinned value.
MODE_KEY = "sandbox.mode"

_RECORD_KEYS = ("schema", "mode", "source", "recorded_at")
_MAX_TIMESTAMP_CHARS = 64


def mode_strength(mode: str, *, local: bool) -> int:
    """What ``mode`` gives a launch of a LOCAL or REMOTE run, as an order.

    0: logical isolation only (``disabled``). 1: enforced where the host can
    be, logical isolation only elsewhere (LOCAL's ``preferred``). 2: never
    run unenforced (``required``, and REMOTE's ``preferred``, D12.2).
    """
    if mode == SANDBOX_REQUIRED:
        return 2
    if mode == SANDBOX_PREFERRED:
        return 1 if local else 2
    return 0


@dataclass(frozen=True)
class SandboxPin:
    """The mode a run is pinned to, and where and when that was decided."""

    mode: str
    source: str
    recorded_at: str

    def direction(self, mode: str, *, local: bool) -> int:
        """-1 when ``mode`` is weaker than the pin, 1 when stronger, 0 when as strong."""
        now, pinned = mode_strength(mode, local=local), mode_strength(self.mode, local=local)
        return (now > pinned) - (now < pinned)

    def describe(self) -> str:
        """The pin as ``status`` and a refusal show it."""
        return f"{self.mode} ({self.source} {self.recorded_at})"

    def to_dict(self) -> dict:
        return {
            "schema": PIN_SCHEMA,
            "mode": self.mode,
            "source": self.source,
            "recorded_at": self.recorded_at,
        }

    def persisted(self) -> dict:
        """:meth:`to_dict`, for the state file: ConfigurationError unless :meth:`from_dict`
        reads it back, so no pin that would make the run unloadable is written."""
        data = self.to_dict()
        try:
            SandboxPin.from_dict(data)
        except StateError as exc:
            raise ConfigurationError(
                f"{exc}: a run pins its sandbox.mode in its state file, which could not be "
                "loaded again otherwise (#280)"
            ) from None
        return data

    @classmethod
    def from_dict(cls, data: Any) -> SandboxPin:
        """Parse a persisted pin strictly; StateError on any other shape."""
        if isinstance(data, dict) and data.get("schema") != PIN_SCHEMA:
            raise StateError(
                f"schema {data.get('schema')!r:.40} is not {PIN_SCHEMA}, the one this "
                "controller reads"
            )
        if not isinstance(data, dict) or sorted(data) != sorted(_RECORD_KEYS):
            raise StateError(f"the sandbox pin must hold exactly {', '.join(_RECORD_KEYS)}")
        mode, source, at = data["mode"], data["source"], data["recorded_at"]
        if mode not in SANDBOX_MODES:
            raise StateError(f"mode must be one of {', '.join(SANDBOX_MODES)}")
        if source not in SOURCES:
            raise StateError(f"source must be one of {', '.join(SOURCES)}")
        if not isinstance(at, str) or not at or len(at) > _MAX_TIMESTAMP_CHARS:
            raise StateError(
                f"recorded_at must be a non-empty string of at most {_MAX_TIMESTAMP_CHARS} "
                "characters"
            )
        return cls(mode=mode, source=source, recorded_at=at)
