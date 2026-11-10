"""The engine's candidate source: where a launch's candidate comes from (ADR 0013 D4, D5; #269).

One abstraction over both backends, so that the engine reads a candidate in
one way whatever ran the agent:

- **The worktree's ``HEAD``** (:class:`WorktreeHead`), for bwrap and
  ``disabled``: unchanged. The candidate is the issue worktree's ``HEAD``
  read through the common dir (ADR 0005 D6.3), after the strict import of
  the private object directory (D6.5); a failed import is the read's
  refusal.
- **The imported artifact** (:class:`ImportedArtifact`), for a backend that
  exports (the container backend, #271): the commit the verify step
  imported from the launch's exported artifact, and no other. The worktree
  is not read: a container's agent never wrote it, and a commit in it is
  never a candidate. A refusal of the verify step is the read's refusal.

**The verify step** of a container launch is :class:`ArtifactCandidateSource`
(the :class:`~autoforge.sandbox_lifecycle.CandidateSource` the lifecycle
calls): it imports the artifact strictly against the launch's git input
(:func:`autoforge.candidate_artifact.import_artifact`), and saves the outcome
in the run's candidate receipts (:class:`CandidateReceipts`) before the
volume it was read from is removed.

**Each phase's bound revision** (ADR 0013 D4; :func:`bound_git_input`), from
which every launch's git input is made, and which nothing re-reads once it
is bound:

- ``ANALYZE_EXECUTE``: the base its entry bound and fetched (the default
  branch head), which is also the lower boundary; carries the entry's last
  imported candidate.
- ``REPLAN_REEXECUTE``: the verified default branch head of its
  transaction, likewise; carries the same.
- ``REVIEW``: the PR HEAD bound to the round and fetched, whoever pushed
  it, above the round's bound merge base; never carries.
- ``FIX``: the reviewed HEAD bound to its entry, above that round's merge
  base; carries the entry's last imported candidate.
- ``UPDATE_EPIC``: the default branch head its entry bound; never carries.
- An export command: the commit it validates, with no lower boundary and
  no history; never carries.

A relaunch of the same entry (a correction, a transient retry, a stop that
``resume`` relaunches) binds the same revision, and so the same
:attr:`~autoforge.git_input.LaunchGitInput.entry`, and seeds from it plus
its carried commit: the ``HEAD`` the controller strictly imported at the end
of an earlier launch of that entry, when it descends from the seed.
Uncommitted changes are never carried, since only an imported commit is.

**The receipts** are a file per phase entry, ``candidates/<entry>.json`` in
the run's state directory, beside ``state.json`` and never part of it, so
that ``state.json``'s protocol does not change (its fields are announced on
#23; no protocol number is claimed). Each holds the entry's launches, in
order: the git input's identity, saved at the end of the seed step before
any journal entry of the launch, and then the verify step's outcome. A
write replaces the file atomically. A file that does not parse is refused,
never read as "no receipt".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .agent_worktree import Candidate, ObjectImportError
from .candidate_artifact import ReadCommit, import_artifact
from .errors import SandboxLaunchRefusedError, StateError
from .executor import ExecutionRequest, ExecutionResult
from .git_input import (
    CARRYING_PHASES,
    BoundRevision,
    GitInputError,
    LaunchGitInput,
)
from .safefs import SafeRoot
from .sandbox_backend import Artifact, LaunchHandle, LaunchOwner
from .sandbox_lifecycle import Verified

Runner = Callable[[ExecutionRequest], ExecutionResult]

RECEIPTS_DIR = "candidates"
RECEIPT_FORMAT = 1
# The launches one entry's receipt keeps: a relaunch reads only the last
# accepted one, and an entry relaunched more often than this is stuck long
# before. The oldest are dropped first.
MAX_RECEIPT_LAUNCHES = 64
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_REFUSAL_CHARS = 2000


def bound_git_input(
    phase: str,
    owner: LaunchOwner,
    *,
    repository: str,
    object_format: str,
    revision: BoundRevision | None,
    carried: str = "",
    export_command: bool = False,
    entry_label: str = "",
) -> LaunchGitInput:
    """The launch's git input from the revision its phase entry bound (D4).

    ``entry_label`` is the engine's name for the phase entry (its review
    round, execution attempt or replan transaction), so that two entries
    binding the same revision never share receipts or a carried commit.

    ``revision`` is ``None`` when the entry bound none: such a launch is
    refused (:class:`SandboxLaunchRefusedError`) before anything of it
    exists, as is a revision that breaks the input's invariants. A carried
    commit is dropped for a phase that never carries one.
    """
    if revision is None:
        raise SandboxLaunchRefusedError(
            f"the {phase} launch has no revision its entry bound, so its seed cannot be "
            "chosen (ADR 0013 D4); nothing of the launch was created"
        )
    if phase not in CARRYING_PHASES or export_command:
        carried = ""
    try:
        return LaunchGitInput(
            phase=phase,
            run=owner.run,
            issue=owner.issue,
            launch=owner.launch,
            repository=repository,
            object_format=object_format,
            seed=revision.seed,
            lower=revision.lower,
            carried=carried,
            export_command=export_command,
            entry_label=entry_label,
        )
    except GitInputError as exc:
        raise SandboxLaunchRefusedError(f"{exc}; nothing of the launch was created") from exc


# -- the receipts ---------------------------------------------------------------------


@dataclass(frozen=True)
class LaunchReceipt:
    """One launch of an entry: its bound git input and, once verified, the outcome."""

    git_input: LaunchGitInput
    verified: bool = False
    candidate: str = ""
    refusal: str = ""

    @property
    def accepted(self) -> bool:
        return self.verified and bool(self.candidate) and not self.refusal

    def to_json(self) -> dict[str, object]:
        return {
            "git_input": self.git_input.identity(),
            "verified": self.verified,
            "candidate": self.candidate,
            "refusal": self.refusal,
        }

    @classmethod
    def from_json(cls, data: object) -> LaunchReceipt:
        if not isinstance(data, dict) or set(data) != {
            "git_input",
            "verified",
            "candidate",
            "refusal",
        }:
            raise StateError("a candidate receipt's launch does not hold exactly its fields")
        if type(data["verified"]) is not bool:
            raise StateError("a candidate receipt's verified is not a boolean")
        for key in ("candidate", "refusal"):
            if not isinstance(data[key], str):
                raise StateError(f"a candidate receipt's {key} is not a string")
        try:
            git_input = LaunchGitInput.from_identity(data["git_input"])
        except (GitInputError, TypeError) as exc:
            raise StateError(f"a candidate receipt's git input is refused: {exc}") from exc
        return cls(git_input, data["verified"], data["candidate"], data["refusal"])


class CandidateReceipts:
    """The run's candidate receipts, one file per phase entry (module docstring).

    ``root`` is the run's state directory as a capability; every read and
    write goes through it, so a link planted there moves no write.
    """

    def __init__(self, root: SafeRoot) -> None:
        self._root = root

    @staticmethod
    def _name(entry: str) -> str:
        return f"{RECEIPTS_DIR}/{entry}.json"

    def launches(self, entry: str) -> list[LaunchReceipt]:
        """The entry's launches, oldest first; ``[]`` when it has none yet."""
        raw = self._root.read_bytes(self._name(entry), limit=MAX_RECEIPT_BYTES)
        if raw is None:
            return []
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise StateError(f"the candidate receipt of entry {entry} does not parse") from exc
        if (
            not isinstance(data, dict)
            or set(data) != {"format", "entry", "launches"}
            or data["format"] != RECEIPT_FORMAT
            or data["entry"] != entry
            or not isinstance(data["launches"], list)
        ):
            raise StateError(
                f"the candidate receipt of entry {entry} is not a format-{RECEIPT_FORMAT} "
                "receipt of that entry"
            )
        launches = [LaunchReceipt.from_json(item) for item in data["launches"]]
        if any(item.git_input.entry != entry for item in launches):
            raise StateError(f"the candidate receipt of entry {entry} holds another entry's launch")
        return launches

    def _write(self, entry: str, launches: list[LaunchReceipt]) -> None:
        data = {
            "format": RECEIPT_FORMAT,
            "entry": entry,
            "launches": [item.to_json() for item in launches[-MAX_RECEIPT_LAUNCHES:]],
        }
        self._root.ensure_dir(RECEIPTS_DIR)
        text = json.dumps(data, sort_keys=True, indent=1) + "\n"
        self._root.write_bytes(self._name(entry), text.encode("utf-8"))

    def bind(self, git_input: LaunchGitInput) -> None:
        """Save the launch's seed identity, at the end of its seed step (D19).

        Idempotent: a launch bound again with the same input changes
        nothing. A launch is its whole input, never its id alone, so an
        input that differs (another carried commit, another seed manifest)
        is a launch of its own and is recorded beside the other.
        """
        launches = self.launches(git_input.entry)
        if any(item.git_input == git_input for item in launches):
            return
        launches.append(LaunchReceipt(git_input))
        self._write(git_input.entry, launches)

    def record(self, git_input: LaunchGitInput, outcome: Verified) -> None:
        """Save the verify step's outcome for the launch ``git_input`` binds.

        The launch's binding is saved first if its seed step saved none.
        Saving the same outcome again changes nothing, so a save repeated
        after a crash is idempotent; another outcome for the same launch
        replaces it, and that launch becomes the entry's latest.
        """
        launches = self.launches(git_input.entry)
        receipt = LaunchReceipt(
            git_input,
            verified=True,
            candidate=outcome.candidate if outcome.accepted else "",
            refusal="" if outcome.accepted else outcome.problem[:MAX_REFUSAL_CHARS],
        )
        if receipt in launches:
            return
        launches = [item for item in launches if item.git_input != git_input]
        launches.append(receipt)
        self._write(git_input.entry, launches)

    def carried(self, entry: str) -> str:
        """The entry's carried commit: its last launch's imported candidate, or ""."""
        for item in reversed(self.launches(entry)):
            if item.verified:
                return item.candidate if item.accepted else ""
        return ""


# -- the verify step ------------------------------------------------------------------


class ArtifactCandidateSource:
    """The verify step for an exported artifact, and its save (ADR 0013 D5, D13, D19).

    :meth:`verify` imports the artifact strictly against the git input the
    launch's handle carries, never against what the artifact says, and
    returns the candidate or a refusal; it never raises for the artifact's
    content. :meth:`save` records the outcome in the receipts.
    """

    def __init__(
        self,
        common: Path,
        runner: Runner | None,
        *,
        read_commit: ReadCommit,
        receipts: CandidateReceipts,
        git_lock: Callable[[str], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._common = common
        self._runner = runner
        self._read_commit = read_commit
        self._receipts = receipts
        self._git_lock = git_lock
        self._verified: tuple[LaunchGitInput, Verified] | None = None

    def verify(self, artifact: Artifact, handle: LaunchHandle) -> Verified:
        git_input = handle.inputs.git_input
        if git_input is None:
            outcome = Verified(
                refusal="the launch bound no git input, so its export is checked against "
                "nothing and is refused"
            )
            self._verified = None
            return outcome
        owner = handle.inputs.owner
        if (owner.run, owner.issue, owner.phase, owner.launch) != (
            git_input.run,
            git_input.issue,
            git_input.phase,
            git_input.launch,
        ):
            outcome = Verified(
                refusal=f"the launch {owner.launch} carries the git input of launch "
                f"{git_input.launch}, so its export is refused"
            )
        else:
            try:
                imported = import_artifact(
                    self._common,
                    Path(artifact.directory),
                    git_input,
                    self._runner,
                    read_commit=self._read_commit,
                    git_lock=self._git_lock,
                )
            except ObjectImportError as exc:
                outcome = Verified(refusal=str(exc))
            else:
                outcome = Verified(candidate=imported.candidate)
        self._verified = (git_input, outcome)
        return outcome

    def save(self, outcome: Verified) -> None:
        if self._verified is None:
            # No git input: nothing to bind the outcome to, and nothing was imported.
            return
        git_input, verified = self._verified
        if outcome != verified:
            raise StateError("the outcome saved is not the one the verify step returned")
        self._receipts.record(git_input, outcome)


# -- the candidate read -------------------------------------------------------------


class CandidateRead(Protocol):
    """Where the engine reads a launch's candidate from (ADR 0013 D5)."""

    def read(self) -> Candidate:
        """The candidate, or the reason there is none (``Candidate.problem``)."""
        ...


@dataclass(frozen=True)
class WorktreeHead:
    """bwrap's and ``disabled``'s candidate: the worktree's ``HEAD`` (ADR 0005 D6.3, D6.5).

    ``import_problem`` is why the strict import after the launch failed, ""
    when it did not: then there is no candidate. ``read_head`` is the read
    through the common dir (:func:`autoforge.agent_worktree.read_candidate`).
    """

    import_problem: str
    read_head: Callable[[], Candidate]

    def read(self) -> Candidate:
        if self.import_problem:
            return Candidate(False, "", self.import_problem)
        return self.read_head()


@dataclass(frozen=True)
class ImportedArtifact:
    """An exporting backend's candidate: the commit the verify step imported, and no other."""

    verified: Verified

    def read(self) -> Candidate:
        if not self.verified.accepted:
            return Candidate(False, "", self.verified.problem)
        return Candidate(False, self.verified.candidate, "")
