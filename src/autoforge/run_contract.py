"""The durable contract of a LOCAL run: what defines it, persisted once.

Why this module exists
----------------------

A LOCAL run has no GitHub to be the source of truth. Its meaning -- *which*
tree is reviewed, *as classified by which rules*, verified by *which*
commands, bounded by *how many* fix rounds, with state kept *where* -- is
decided when the run is created, from the configuration and the environment
of that moment. Every later step of the run (a ``step``, a ``resume``, a
crash recovery, a ``status``) runs in a new process that loads the state
file and *also* loads today's configuration and observes today's
environment. The failure mode this module closes is letting those two
sources be confused: a step that re-derives a run-defining input from the
current environment and treats the result as the run's definition has
silently **rebound** the run. Review rounds 6 and 7 each found one instance
of that pattern (``local.exclude`` re-read on resume; the state directory
re-resolved by pathname at bootstrap) and each fix froze one input. That
cannot converge: every un-enumerated input is the next finding.

The closed formulation is a contract:

* :class:`WorkspacePolicy` -- everything about the workspace reader that
  shapes a snapshot, in one canonical, unambiguous, digested form.
* :class:`LocalRunContract` -- every run-defining input, classified
  **IMMUTABLE** (persisted at creation; a later invocation must present the
  same value or the run refuses to continue).
* :func:`validate_local_run_contract` -- the single gate that compares the
  persisted contract with what the current invocation would define, before
  anything executes.

The master invariant: *a resumed run may revalidate its contract, but must
never silently redefine it.* Concretely, execution reads the run-defining
values **from the contract** (``contract.validation_commands``, never
``config.local.validation_commands``), and the gate is what proves the
operator's current configuration still agrees with it. Adding a
run-defining input is one field on :class:`LocalRunContract`; the
persistence, the comparison and the drift message follow from the
dataclass, so there is no second list to forget.

Classification of the run's inputs
----------------------------------

IMMUTABLE (in the contract, compared by the gate, drift refuses):
    repository root, state root, workspace policy (exclusion rules, cost
    bounds, snapshot algorithm), validation commands, fix-round budget,
    cumulative step budget, prompt version. Also, outside the contract object but with the same
    semantics: the protocol version (checked on load) and the feature
    specification path (persisted; re-supplied by nothing, so it cannot
    drift).

REVALIDATED (re-read from the environment and proven equivalent):
    the feature specification's bytes (SHA-256, VerificationError), the git
    anchor (HEAD and branch, BLOCKED). These are *content* checks of the
    world the contract names; they are performed where the run touches that
    content, and they never write what they read back into the contract.

DYNAMIC (observed, never defining):
    the pre-phase workspace fingerprint (a binding for the *next* agent
    phase, persisted so the reviewer's claim can be checked against it -- a
    persisted safety judgment never reads a stale one back), git-directory
    inodes (re-read per snapshot from a location the contract names),
    provider/model/effort/command/extra_args/timeouts (which agent runs, not
    what it is asked to do), the invocation's cwd and PATH, counters. No
    persisted safety judgment depends on any of them, which is the proof
    that they may change.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass, field, fields
from typing import TYPE_CHECKING, Any

from . import __prompt_version__
from .errors import StateError, VerificationError

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .config import AutoForgeConfig
    from .local_workspace import LocalWorkspace
    from .state import StatePaths

#: Schema of the persisted contract. Bumped when a field is added, removed or
#: reinterpreted; an older schema is refused, never migrated (see
#: :meth:`LocalRunContract.from_dict`). 2 added ``max_total_steps``: a run
#: created under schema 1 could resume under a larger cumulative step budget
#: than the one it was created with, without any drift being reported.
CONTRACT_SCHEMA = 2

#: Schema of the persisted workspace policy. ``v1`` (the pre-release form:
#: one comma-joined line that did not name the snapshot algorithm and could
#: not tell ``["a,b", "c"]`` from ``["a", "b,c"]``) is refused.
POLICY_SCHEMA = "v2"

_POLICY_FIELDS = ("snapshot_tag", "exclude", "max_entries", "max_bytes")
_SNAPSHOT_TAG_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fmt(value: Any) -> str:
    """Render a contract value for a drift line (JSON, so [] and "" are visible)."""
    if isinstance(value, tuple):
        return json.dumps([list(v) if isinstance(v, tuple) else v for v in value])
    return json.dumps(value)


@dataclass(frozen=True)
class WorkspacePolicy:
    """Everything about the workspace reader that shapes a snapshot.

    A fingerprint binds the working tree *as this reader classifies it*, so
    the reader's configuration is part of what a review means. Comparing
    fingerprints cannot detect a policy change: a re-snapshot after the
    change computes both sides under the new policy. The policy is therefore
    part of the run's contract, whole: the bounds only ever turn a snapshot
    into a refusal, so carving them out would be sound today -- and would be
    one more enumeration of which knobs happen to be benign, the shape of
    reasoning this design replaced. The snapshot algorithm's tag is in it
    for the same reason: a controller whose walk classifies entries
    differently produces fingerprints that mean something else.
    """

    exclude: tuple[str, ...]
    max_entries: int
    max_bytes: int
    snapshot_tag: str

    def __post_init__(self) -> None:
        # The policy is a *set* of rules: one canonical spelling per policy,
        # so equality, the digest and the drift message never depend on the
        # order the operator wrote the list in.
        object.__setattr__(self, "exclude", tuple(sorted(set(self.exclude))))

    def canonical(self) -> str:
        """The one canonical spelling of this policy; what is digested.

        A JSON object with sorted keys and no whitespace. Every exclusion
        pattern is its own JSON string, so the encoding is unambiguous for
        any pattern the loader accepts: a comma, a bracket or a quote inside
        a pattern is quoted, never a delimiter. (The pre-release ``v1`` text
        joined the patterns with commas, and ``["a,b", "c"]`` and
        ``["a", "b,c"]`` were one policy under it.)
        """
        return json.dumps(
            {
                "version": POLICY_SCHEMA,
                "snapshot_tag": self.snapshot_tag,
                "exclude": list(self.exclude),
                "max_entries": self.max_entries,
                "max_bytes": self.max_bytes,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def digest(self) -> str:
        return _digest(self.canonical())

    def to_dict(self) -> dict:
        """The persisted form: the fields, readable as they are, plus the digest."""
        return {
            "version": POLICY_SCHEMA,
            "snapshot_tag": self.snapshot_tag,
            "exclude": list(self.exclude),
            "max_entries": self.max_entries,
            "max_bytes": self.max_bytes,
            "digest": self.digest(),
        }

    @classmethod
    def from_dict(cls, data: Any) -> WorkspacePolicy:
        """Strictly decode the persisted form; the digest must match the fields.

        The fields are required to be *already canonical* (sorted, unique,
        loader-normalised patterns): a policy the controller wrote is, and
        one that is not was written by something else.
        """
        expected = {"version", "digest", *_POLICY_FIELDS}
        if not isinstance(data, dict) or set(data) != expected:
            raise StateError(
                "workspace policy must be an object with exactly the fields "
                + ", ".join(sorted(expected))
            )
        if data["version"] != POLICY_SCHEMA:
            raise StateError(
                f"workspace policy version {data['version']!r} is not {POLICY_SCHEMA!r}; "
                "this run was created by a different controller release and is not "
                "migrated -- start a new run"
            )
        tag = data["snapshot_tag"]
        if not isinstance(tag, str) or not _SNAPSHOT_TAG_RE.match(tag):
            raise StateError(f"workspace policy snapshot tag is not usable: {tag!r}")
        exclude = data["exclude"]
        if (
            not isinstance(exclude, list)
            or not all(isinstance(p, str) and p and "\0" not in p for p in exclude)
            or exclude != sorted(set(exclude))
        ):
            raise StateError(f"workspace policy exclusions are not canonical: {exclude!r}")
        bounds = {}
        for name in ("max_entries", "max_bytes"):
            value = data[name]
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise StateError(f"workspace policy field {name!r} must be a non-negative integer")
            bounds[name] = value
        if not isinstance(data["digest"], str):
            raise StateError("workspace policy digest must be a string")
        policy = cls(exclude=tuple(exclude), snapshot_tag=tag, **bounds)
        if data["digest"] != policy.digest():
            raise StateError("workspace policy digest does not match its fields")
        return policy

    def drift(self, current: WorkspacePolicy) -> list[str]:
        """Per-field differences, named by the config setting that moves them."""
        lines: list[str] = []
        labels = {
            "exclude": "local.exclude",
            "max_entries": "local.max_workspace_entries",
            "max_bytes": "local.max_workspace_bytes",
            "snapshot_tag": "workspace snapshot algorithm",
        }
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(current, f.name)
            if mine != theirs:
                lines.append(f"{labels[f.name]}: run: {_fmt(mine)} current: {_fmt(theirs)}")
        return lines


@dataclass(frozen=True)
class LocalRunContract:
    """The persisted definition of one LOCAL run (every field IMMUTABLE).

    Each field carries the name the operator knows it by, so a drift line
    reads ``local.exclude: run: [] current: ["src"]``.
    """

    repository_root: str = field(metadata={"label": "repository root"})
    state_root: str = field(metadata={"label": "state directory"})
    workspace_policy: WorkspacePolicy = field(metadata={"label": "workspace policy"})
    validation_commands: tuple[tuple[str, ...], ...] = field(
        metadata={"label": "local.validation_commands"}
    )
    max_fix_rounds: int = field(metadata={"label": "local.max_fix_rounds"})
    # The run's cumulative step budget. It bounds the run as a whole (every
    # phase entry, across `resume`), so it is as run-defining as the fix
    # budget: enforced from here, never from the configuration of the day.
    max_total_steps: int = field(metadata={"label": "workflow.max_total_steps"})
    prompt_version: str = field(metadata={"label": "prompt_version"})

    @property
    def max_review_rounds(self) -> int:
        """Review passes the run may complete: the fix rounds plus the first."""
        return self.max_fix_rounds + 1

    # -- construction ------------------------------------------------------
    @classmethod
    def from_config(
        cls, config: AutoForgeConfig, workspace: LocalWorkspace, paths: StatePaths
    ) -> LocalRunContract:
        """What *this* invocation would define the run as.

        Used once to create the contract and afterwards only to compare
        against it; a comparison never writes anything back.
        """
        return cls(
            repository_root=str(workspace.root()),
            state_root=paths.canonical_state_dir(),
            workspace_policy=workspace.policy(),
            validation_commands=tuple(
                tuple(str(a) for a in argv) for argv in config.local.validation_commands
            ),
            max_fix_rounds=int(config.local.max_fix_rounds),
            max_total_steps=int(config.workflow.max_total_steps),
            prompt_version=config.prompt_version or __prompt_version__,
        )

    # -- persistence -------------------------------------------------------
    def to_dict(self) -> dict:
        return {
            "schema": CONTRACT_SCHEMA,
            "repository_root": self.repository_root,
            "state_root": self.state_root,
            "workspace_policy": self.workspace_policy.to_dict(),
            "validation_commands": [list(argv) for argv in self.validation_commands],
            "max_fix_rounds": self.max_fix_rounds,
            "max_total_steps": self.max_total_steps,
            "prompt_version": self.prompt_version,
        }

    @classmethod
    def from_dict(cls, data: Any) -> LocalRunContract:
        """Strict decode. Missing, extra or mistyped fields are StateError.

        There is deliberately no ``missing field -> fill from current
        config`` path: that would be the silent rebinding this contract
        exists to make impossible. A contract an older controller wrote is
        refused with a message that says to start a new run.
        """
        if not isinstance(data, dict):
            raise StateError("local run contract must be an object")
        expected = {"schema"} | {f.name for f in fields(cls)}
        missing = sorted(expected - set(data))
        unknown = sorted(set(data) - expected)
        if missing or unknown:
            raise StateError(
                "local run contract has "
                + (f"missing field(s) {missing}" if missing else "")
                + (" and " if missing and unknown else "")
                + (f"unknown field(s) {unknown}" if unknown else "")
                + "; a contract the controller cannot read whole is not reconstructed from "
                "the current configuration -- start a new run"
            )
        if data["schema"] != CONTRACT_SCHEMA:
            raise StateError(
                f"local run contract schema {data['schema']!r} is not {CONTRACT_SCHEMA}; "
                "this run was created by a different controller release and is not "
                "migrated -- start a new run"
            )
        for name in ("repository_root", "state_root", "prompt_version"):
            if not isinstance(data[name], str) or not data[name]:
                raise StateError(f"local run contract field {name!r} must be a non-empty string")
        rounds = data["max_fix_rounds"]
        if not isinstance(rounds, int) or isinstance(rounds, bool) or rounds < 0:
            raise StateError(
                "local run contract field 'max_fix_rounds' must be a non-negative integer"
            )
        steps = data["max_total_steps"]
        if not isinstance(steps, int) or isinstance(steps, bool) or steps < 1:
            # The loader refuses a budget below 1, so a persisted one is
            # something the controller never wrote.
            raise StateError(
                "local run contract field 'max_total_steps' must be a positive integer"
            )
        commands = data["validation_commands"]
        if not isinstance(commands, list) or not all(
            isinstance(argv, list) and argv and all(isinstance(a, str) for a in argv)
            for argv in commands
        ):
            raise StateError(
                "local run contract field 'validation_commands' must be a list of "
                "non-empty argv lists of strings"
            )
        return cls(
            repository_root=data["repository_root"],
            state_root=data["state_root"],
            workspace_policy=WorkspacePolicy.from_dict(data["workspace_policy"]),
            validation_commands=tuple(tuple(argv) for argv in commands),
            max_fix_rounds=rounds,
            max_total_steps=steps,
            prompt_version=data["prompt_version"],
        )

    # -- comparison --------------------------------------------------------
    def drift(self, current: LocalRunContract) -> list[str]:
        """Every field on which ``current`` disagrees with this contract.

        Generic over the dataclass: a new field is compared and reported
        without a new branch here.
        """
        lines: list[str] = []
        for f in fields(self):
            mine, theirs = getattr(self, f.name), getattr(current, f.name)
            if isinstance(mine, WorkspacePolicy):
                lines.extend(mine.drift(theirs))
            elif mine != theirs:
                lines.append(f"{f.metadata['label']}: run: {_fmt(mine)} current: {_fmt(theirs)}")
        return lines


def contract_drift_error(lines: Sequence[str]) -> VerificationError:
    return VerificationError(
        "this LOCAL run's contract does not match the current invocation:\n  "
        + "\n  ".join(lines)
        + "\nA run is defined once, when it is created; resuming it under a different "
        "definition would silently rebind what was reviewed, verified or bounded. Restore "
        "the run's settings (and location) to resume it, or start a new run under the "
        "current ones. Nothing was changed."
    )


def validate_local_run_contract(
    recorded: LocalRunContract, current: LocalRunContract
) -> LocalRunContract:
    """The gate: refuse (VerificationError, per-field) unless the two agree.

    Returns the recorded contract, which is what execution then reads from.
    Nothing is persisted on either outcome.
    """
    lines = recorded.drift(current)
    if lines:
        raise contract_drift_error(lines)
    return recorded
