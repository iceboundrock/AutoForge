"""The configuration a REMOTE run was recorded under (#219).

Why this module exists
----------------------

The CLI has no configuration discovery. A REMOTE run started with
``--config`` and resumed without it runs on the built-in defaults: another
model and effort, other limits, and an agent worktree under
``<git common dir>/autoforge/worktrees`` instead of the configured
``execution.worktree_dir`` -- a different agent in a different, empty
worktree. A LOCAL run has its contract for what defines it
(:mod:`autoforge.run_contract`); this is the REMOTE counterpart for what
decides *who* works on the run and *where*.

A REMOTE run records a :class:`RecordedConfiguration` when it is created
(``state.config_fingerprint``), and ``resume``, ``step`` and ``unblock``
compare the configuration they would run on with it before any phase
executes (:meth:`autoforge.engine.ControllerEngine.check_run_configuration`).
A difference refuses, naming each changed key, unless the operator opts in
with ``--accept-config-change``; the change is then written to the run log
and the new configuration replaces the recorded one.

What it holds
-------------

For every profile a REMOTE run can route to (:data:`FINGERPRINT_PROFILES`):
the provider, the model, the effort and the limits the agent runs under
(the profile's own ``idle_timeout_seconds`` / ``max_runtime_seconds``, else
the ``execution.default_*`` value); and the resolved agent worktree root.
Beside them, the absolute path of the ``--config`` file it was recorded from
(``""`` for the built-in defaults), so a refusal can say how to restore it.

Nothing else is recorded, and nothing that could be a secret: a profile's
``command``, ``extra_args`` and ``options`` and the environment are left out
(an argument or an option is where a key would go), so the routing fields
are stored and printed as they are, and every text built from them crosses
the redaction boundary at the CLI and the run log anyway.

Which keys must match
---------------------

Every key recorded here. Provider, model, effort and the worktree root
decide which agent works and in which worktree; the limits are recorded
because a ``resume`` without ``--config`` changes them just as silently.
Raising a limit after a timeout is a legitimate reason to resume on a
changed configuration, and that is what the opt-in is for: it is one flag,
and the change is recorded.

Everything outside the fingerprint may change between steps without it:
loop bounds and the step budget, correction attempts, the merge gate and
its verification commands, the environment allow-list, the GitHub client,
and ``sandbox.mode`` (a run resumes under either mode, ADR 0005 D15.3).
They bound, verify or contain what an agent does without choosing the
agent or its worktree, and each is read at the step that uses it.

A state file written before this field existed has none: it loads, is not
checked, and the first ``resume``, ``step`` or ``unblock`` records the
configuration it runs on (source ``upgraded``), saved with that command's
next save.

What can be recorded
--------------------

What the record's parser (:meth:`RecordedConfiguration.from_dict`) reads
back, and nothing else: it runs on every load of the state file, so a
record it refuses would make the run unloadable. The configuration bounds
less than the parser does (a model of any length; an ``update_epic``
profile, which no command validates before the run reaches it, with an
empty provider or a zero limit), so the parser is also run on what is
about to be recorded, and a value it refuses is a :class:`ConfigurationError`
before anything is written. ``run`` refuses a routing at config validation
(:func:`fingerprint_routing`; its dry run and ``doctor`` too) and a path
when it records the run (:meth:`RecordedConfiguration.persisted`), and
``resume``, ``step`` and ``unblock`` refuse to record or accept either.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .errors import ConfigurationError, StateError
from .profiles import REQUIRED_PROFILES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import AutoForgeConfig, ProfileConfig

#: Schema of the persisted record. Bumped when a recorded key is added,
#: removed or reinterpreted; another schema is refused on load, never
#: migrated (a recorded configuration rebuilt from today's is no record).
FINGERPRINT_SCHEMA = 1

#: Every profile a REMOTE run can route to: the ones a run requires, and
#: ``update_epic``, which the run reaches after a merge. A profile the
#: configuration does not define is recorded as ``None``.
FINGERPRINT_PROFILES: tuple[str, ...] = (*REQUIRED_PROFILES, "update_epic")

#: How the record came to be: ``created`` by ``run``; ``upgraded`` by the
#: first command that found a state file written before #219 without one;
#: ``accepted`` by an operator's ``--accept-config-change``.
SOURCE_CREATED = "created"
SOURCE_UPGRADED = "upgraded"
SOURCE_ACCEPTED = "accepted"
SOURCES = (SOURCE_CREATED, SOURCE_UPGRADED, SOURCE_ACCEPTED)

#: What a refusal and the run log call the worktree root.
WORKTREE_ROOT_KEY = "agent worktree root (execution.worktree_dir)"

_RECORD_KEYS = ("schema", "profiles", "worktree_root", "config_file", "source", "recorded_at")
_PROFILE_KEYS = ("provider", "model", "effort", "idle_timeout_seconds", "max_runtime_seconds")
# Bounds on what a hand-edited file can make the controller carry and print.
_MAX_NAME_CHARS = 512
_MAX_PATH_CHARS = 4096
_MAX_TIMESTAMP_CHARS = 64


def _text(value: Any, where: str, *, bound: int, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > bound or (not empty and not value):
        shape = "a string" if empty else "a non-empty string"
        raise StateError(f"{where} must be {shape} of at most {bound} characters")
    return value


def _limit(value: Any, where: str, *, optional: bool) -> int | None:
    if value is None and optional:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        shape = "a positive integer or null" if optional else "a positive integer"
        raise StateError(f"{where} must be {shape}")
    return value


def _path(value: Any, where: str, *, empty: bool = False) -> str:
    text = _text(value, where, bound=_MAX_PATH_CHARS, empty=empty)
    if text and not os.path.isabs(text):
        raise StateError(f"{where} must be an absolute path")
    return text


def _keys(data: Any, keys: tuple[str, ...], where: str) -> dict:
    if not isinstance(data, dict) or sorted(data) != sorted(keys):
        raise StateError(f"{where} must hold exactly {', '.join(keys)}")
    return data


def _recordable(parse: Callable[[], object]) -> None:
    """Refuse (ConfigurationError) what is about to be recorded unless ``parse`` reads it back.

    ``parse`` is the record's own parser run on it, the one definition of
    what a state file may hold here; its refusal names the key, never the
    value.
    """
    try:
        parse()
    except StateError as exc:
        raise ConfigurationError(
            f"{exc}: a REMOTE run records it in its state file, which could not be loaded "
            "again otherwise (#219)"
        ) from None


@dataclass(frozen=True)
class ProfileRouting:
    """What one profile decides about the agent it launches."""

    provider: str
    model: str
    effort: str
    idle_timeout_seconds: int
    max_runtime_seconds: int | None

    @classmethod
    def from_config(cls, cfg: AutoForgeConfig, profile: ProfileConfig) -> ProfileRouting:
        """The profile's routing, its limits resolved against the ``execution`` defaults.

        Resolved here rather than through ``AutoForgeConfig.agent_limits``,
        which also refuses a provider that cannot report activity: recording
        is not the place to refuse a profile, and the configured values are
        what an operator changes. The one refusal is a routing its record
        could not be read back with (ConfigurationError, see "What can be
        recorded" above).
        """
        idle = profile.idle_timeout_seconds
        ceiling = profile.max_runtime_seconds
        routing = cls(
            provider=profile.provider,
            model=profile.model,
            effort=profile.effort,
            idle_timeout_seconds=(
                idle if idle is not None else cfg.execution.default_idle_timeout_seconds
            ),
            max_runtime_seconds=(
                ceiling if ceiling is not None else cfg.execution.default_max_runtime_seconds
            ),
        )
        _recordable(lambda: cls.from_dict(routing.to_dict(), f"profiles.{profile.name}"))
        return routing

    def to_dict(self) -> dict:
        return {key: getattr(self, key) for key in _PROFILE_KEYS}

    @classmethod
    def from_dict(cls, data: Any, where: str) -> ProfileRouting:
        data = _keys(data, _PROFILE_KEYS, where)
        return cls(
            provider=_text(data["provider"], f"{where}.provider", bound=_MAX_NAME_CHARS),
            model=_text(data["model"], f"{where}.model", bound=_MAX_NAME_CHARS, empty=True),
            effort=_text(data["effort"], f"{where}.effort", bound=_MAX_NAME_CHARS, empty=True),
            idle_timeout_seconds=_limit(  # type: ignore[arg-type]
                data["idle_timeout_seconds"], f"{where}.idle_timeout_seconds", optional=False
            ),
            max_runtime_seconds=_limit(
                data["max_runtime_seconds"], f"{where}.max_runtime_seconds", optional=True
            ),
        )


@dataclass(frozen=True)
class ConfigChange:
    """One recorded key whose value this invocation would change."""

    key: str
    recorded: str
    current: str

    def line(self) -> str:
        return f"{self.key}: {self.recorded} -> {self.current}"

    def to_dict(self) -> dict:
        return {"key": self.key, "recorded": self.recorded, "current": self.current}


def fingerprint_routing(cfg: AutoForgeConfig) -> dict[str, ProfileRouting | None]:
    """The routing of every profile in :data:`FINGERPRINT_PROFILES`, ``None`` when not configured.

    ConfigurationError when one could not be recorded. A REMOTE run's
    ``validate_config`` calls it, so ``run`` refuses such a configuration
    before the run is created, its dry run included.
    """
    return {
        name: ProfileRouting.from_config(cfg, cfg.profiles[name]) if name in cfg.profiles else None
        for name in FINGERPRINT_PROFILES
    }


def _shown(value: Any) -> str:
    """A recorded value as a refusal prints it (JSON, so "" and null are visible)."""
    return json.dumps(value)


def _shown_profile(routing: ProfileRouting | None) -> str:
    return "(not configured)" if routing is None else _shown(routing.to_dict())


@dataclass(frozen=True)
class ConfigFingerprint:
    """The compared part: per-profile routing and the agent worktree root."""

    profiles: dict[str, ProfileRouting | None]
    worktree_root: str

    @classmethod
    def from_config(cls, cfg: AutoForgeConfig, worktree_root: str | Path) -> ConfigFingerprint:
        """ConfigurationError when a routing or the root could not be recorded."""
        root = str(worktree_root)
        profiles = fingerprint_routing(cfg)
        _recordable(lambda: _path(root, WORKTREE_ROOT_KEY))
        return cls(profiles=profiles, worktree_root=root)

    def changes(self, current: ConfigFingerprint) -> list[ConfigChange]:
        """Each key ``current`` would change, in a stable order; empty when none."""
        out: list[ConfigChange] = []
        for name in FINGERPRINT_PROFILES:
            was, now = self.profiles.get(name), current.profiles.get(name)
            if was == now:
                continue
            if was is None or now is None:
                out.append(
                    ConfigChange(f"profiles.{name}", _shown_profile(was), _shown_profile(now))
                )
                continue
            for key in _PROFILE_KEYS:
                before, after = getattr(was, key), getattr(now, key)
                if before != after:
                    out.append(
                        ConfigChange(f"profiles.{name}.{key}", _shown(before), _shown(after))
                    )
        if self.worktree_root != current.worktree_root:
            out.append(
                ConfigChange(
                    WORKTREE_ROOT_KEY, _shown(self.worktree_root), _shown(current.worktree_root)
                )
            )
        return out


@dataclass(frozen=True)
class RecordedConfiguration:
    """The fingerprint as persisted, with where and how it was recorded."""

    fingerprint: ConfigFingerprint
    # The absolute path of the ``--config`` file it was recorded from; ""
    # for the built-in defaults. Not compared: a moved file with the same
    # content is the same configuration.
    config_file: str
    source: str
    recorded_at: str

    def to_dict(self) -> dict:
        return {
            "schema": FINGERPRINT_SCHEMA,
            "profiles": {
                name: (None if routing is None else routing.to_dict())
                for name, routing in self.fingerprint.profiles.items()
            },
            "worktree_root": self.fingerprint.worktree_root,
            "config_file": self.config_file,
            "source": self.source,
            "recorded_at": self.recorded_at,
        }

    def persisted(self) -> dict:
        """:meth:`to_dict`, for the state file: ConfigurationError unless :meth:`from_dict`
        reads it back, so no record that would make the run unloadable is written."""
        data = self.to_dict()
        _recordable(lambda: RecordedConfiguration.from_dict(data))
        return data

    @classmethod
    def from_dict(cls, data: Any) -> RecordedConfiguration:
        """Parse a persisted record strictly; StateError on any other shape."""
        if isinstance(data, dict) and data.get("schema") != FINGERPRINT_SCHEMA:
            raise StateError(
                f"schema {data.get('schema')!r:.40} is not {FINGERPRINT_SCHEMA}, the one this "
                "controller writes; a recorded configuration is never migrated"
            )
        data = _keys(data, _RECORD_KEYS, "the recorded configuration")
        raw_profiles = _keys(data["profiles"], FINGERPRINT_PROFILES, "profiles")
        profiles = {
            name: (
                None
                if raw_profiles[name] is None
                else ProfileRouting.from_dict(raw_profiles[name], f"profiles.{name}")
            )
            for name in FINGERPRINT_PROFILES
        }
        source = data["source"]
        if source not in SOURCES:
            raise StateError(f"source must be one of {', '.join(SOURCES)}")
        return cls(
            fingerprint=ConfigFingerprint(
                profiles=profiles, worktree_root=_path(data["worktree_root"], "worktree_root")
            ),
            config_file=_path(data["config_file"], "config_file", empty=True),
            source=source,
            recorded_at=_text(data["recorded_at"], "recorded_at", bound=_MAX_TIMESTAMP_CHARS),
        )

    def origin(self) -> str:
        """Where it was recorded from, for a message: the file, or the built-in defaults."""
        return f"--config {self.config_file}" if self.config_file else "the built-in defaults"
