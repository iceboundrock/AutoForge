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
  never a candidate. A refusal of the verify step is the read's refusal,
  and so is a launch that exported nothing (:data:`NOT_EXPORTED`): which
  of the two sources a launch reads is chosen from its backend's contract
  before it runs, never from whether a verify outcome exists.

**The verify step** of a container launch is :class:`ArtifactCandidateSource`
(the :class:`~autoforge.sandbox_lifecycle.CandidateSource` the lifecycle
calls): it imports the artifact strictly against the launch's git input
(:func:`autoforge.candidate_artifact.import_artifact`), and saves the outcome
in the run's candidate receipts (:class:`CandidateReceipts`) before the
volume it was read from is removed. An export that failed is saved the same
way, as the launch's refusal.

**The artifact's origin** is authenticated before a byte of it is read. The
controller chooses each launch's export directory itself, fresh and under
its own sandbox directories (:func:`export_location`:
``<git common dir>/autoforge/sandbox/<key>/export/<launch id>-<token>``,
ADR 0005 D8.4 as amended, with a random token so that no earlier launch
ever used the path), and hands it to the launch (``LaunchInputs.export_dir``).
The lifecycle journals the exporter's export directory only when it is that
path, and the export step's outcome names the object it journaled
(``Exported.origin``). The verify step accepts an artifact only when that
journaled object is the launch's own, names the launch's export directory,
and the artifact is in it; and it opens that directory from the common dir
one component at a time, never following a link. An artifact anywhere else
(another path, another launch's export directory, a directory planted
beside it) is refused whatever its manifest says, and nothing is published.

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
of the latest earlier launch of that entry whose artifact it accepted, when
it descends from the seed. A later launch whose artifact was refused
imported nothing, so it takes nothing away. Uncommitted changes are never
carried, since only an imported commit is. An ``ANALYZE_EXECUTE`` entry
that found earlier work on the issue's branch (a branch head, or the PR it
adopts) carries that head, verified by the entry and descending from the
base, until a launch of the entry imported a commit above it.

**The receipts** are a file per phase entry, ``candidates/<entry>.json`` in
the run's state directory, beside ``state.json`` and never part of it, so
that ``state.json``'s protocol does not change (its fields are announced on
#23; no protocol number is claimed). Each holds the entry's launches, in
order: the git input's identity, saved at the end of the seed step before
any journal entry of the launch, and then the verify step's outcome. A
write replaces the file atomically. A file that does not parse is refused,
never read as "no receipt". A refusal can quote what the controller does not
write (an exporter's failure, a runtime's message, the paths an artifact's
origin named, the import's diagnostics), so a receipt keeps it redacted
whole and only then cut to :data:`MAX_REFUSAL_CHARS` (:func:`receipt_refusal`),
as every writer of persisted text does
(``docs/agent-guides/secrets-and-logging.md``).
"""

from __future__ import annotations

import json
import re
import secrets
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
from .redaction import redact
from .safefs import SafeRoot
from .sandbox_backend import LaunchHandle, LaunchOwner, ObjectKind
from .sandbox_lifecycle import Exported, Verified

Runner = Callable[[ExecutionRequest], ExecutionResult]

RECEIPTS_DIR = "candidates"
RECEIPT_FORMAT = 1
# The launches one entry's receipt keeps: a relaunch reads only the last
# accepted one, and an entry relaunched more often than this is stuck long
# before. The oldest are dropped first.
MAX_RECEIPT_LAUNCHES = 64
MAX_RECEIPT_BYTES = 1024 * 1024
MAX_REFUSAL_CHARS = 2000
# The random part of a launch's export directory's name (:func:`export_location`).
_EXPORT_TOKEN_HEX = 16


def export_location(root: Path, launch: str) -> str:
    """A fresh export directory for ``launch`` under ``root``, the controller's (D8.4).

    ``root`` is ``<git common dir>/autoforge/sandbox/<key>/export``. The name
    is the launch's id and a random token, so no earlier launch, of this
    entry or another, ever wrote that path: a leftover or planted artifact
    is never at it.
    """
    return str(root / f"{launch}-{secrets.token_hex(_EXPORT_TOKEN_HEX // 2)}")


def export_leaf(root: Path, path: str, launch: str) -> str:
    """The name of ``path`` under ``root`` when it is one of ``launch``'s export directories.

    "" for any other path: another directory, a deeper one, another
    launch's, or a name :func:`export_location` never makes.
    """
    parent, _, leaf = path.rpartition("/")
    pattern = re.escape(launch) + f"-[0-9a-f]{{{_EXPORT_TOKEN_HEX}}}"
    if parent != str(root) or not re.fullmatch(pattern, leaf):
        return ""
    return leaf


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


def receipt_refusal(problem: str) -> str:
    """``problem`` as a receipt keeps it: redacted whole, then cut to :data:`MAX_REFUSAL_CHARS`.

    In that order: the cut is by character count and knows nothing about
    secrets, so a credential it cut in half would no longer match its
    pattern and its head would be kept in the clear. Redaction can lengthen
    the text (:data:`~autoforge.redaction.MAX_GROWTH_FACTOR`); the cut after
    it holds the bound all the same.
    """
    return redact(problem)[:MAX_REFUSAL_CHARS]


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
        kept = launches[-MAX_RECEIPT_LAUNCHES:]
        accepted = [item for item in launches if item.accepted]
        if accepted and accepted[-1] not in kept:
            # The latest accepted launch is the entry's carried commit: never
            # dropped for the refused ones after it.
            kept = [accepted[-1], *launches[-(MAX_RECEIPT_LAUNCHES - 1) :]]
        data = {
            "format": RECEIPT_FORMAT,
            "entry": entry,
            "launches": [item.to_json() for item in kept],
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
        replaces it, and that launch becomes the entry's latest. A refusal
        is saved redacted, then bounded (:func:`receipt_refusal`): the
        receipt is written in the clear, and the refusal can quote an
        exporter's or a runtime's output.
        """
        launches = self.launches(git_input.entry)
        receipt = LaunchReceipt(
            git_input,
            verified=True,
            candidate=outcome.candidate if outcome.accepted else "",
            refusal="" if outcome.accepted else receipt_refusal(outcome.problem),
        )
        if receipt in launches:
            return
        launches = [item for item in launches if item.git_input != git_input]
        launches.append(receipt)
        self._write(git_input.entry, launches)

    def carried(self, entry: str) -> str:
        """The entry's carried commit: its latest accepted launch's imported candidate, or "".

        A launch whose artifact was refused (or whose export failed)
        imported nothing, so a later refusal never takes away a commit an
        earlier launch of the entry imported strictly (ADR 0013 D4).
        """
        for item in reversed(self.launches(entry)):
            if item.accepted:
                return item.candidate
        return ""


# -- the verify step ------------------------------------------------------------------


class ArtifactCandidateSource:
    """The verify step for an exported artifact, and its save (ADR 0013 D5, D13, D17, D19).

    :meth:`verify` takes the export step's outcome: an export that failed
    is the launch's refusal; an artifact is accepted only from the launch's
    own export directory (the module docstring's origin), then imported
    strictly against the git input the launch's handle carries, never
    against what the artifact says. It returns the candidate or a refusal,
    and never raises for the artifact's content. :meth:`save` records the
    outcome against that git input in the receipts, a refusal included
    (redacted, then bounded: :func:`receipt_refusal`).

    ``common`` is the common dir and ``export_root`` the launch's export
    directories' parent, relative to it
    (``autoforge/sandbox/<key>/export``).
    """

    def __init__(
        self,
        common: Path,
        runner: Runner | None,
        *,
        read_commit: ReadCommit,
        receipts: CandidateReceipts,
        export_root: str,
        git_lock: Callable[[str], AbstractContextManager[object]] | None = None,
    ) -> None:
        self._common = common
        self._runner = runner
        self._read_commit = read_commit
        self._receipts = receipts
        self._export_root = export_root
        self._git_lock = git_lock
        self._verified: tuple[LaunchGitInput, Verified] | None = None

    def verify(self, exported: Exported, handle: LaunchHandle) -> Verified:
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
        elif exported.artifact is None:
            # D17: a failed export is the verify step's refusal, saved as one.
            outcome = Verified(
                refusal="the launch's export failed: "
                + (exported.failure or "its exporter left no artifact")
            )
        else:
            outcome = self._import(exported, handle, git_input)
        self._verified = (git_input, outcome)
        return outcome

    def _origin_problem(self, exported: Exported, handle: LaunchHandle) -> str:
        """Why ``exported`` is not from the launch's own export directory, or ""."""
        owner = handle.inputs.owner
        expected = handle.inputs.export_dir
        origin = exported.origin
        if origin is None or origin.kind is not ObjectKind.EXPORT_DIR or origin.owner != owner:
            return "it is not in an export directory the launch's lifecycle journaled for it"
        if not expected or origin.name != expected:
            return (
                f"the export directory journaled for the launch is {origin.name}, not the "
                f"one the controller chose for it ({expected or 'none'})"
            )
        assert exported.artifact is not None
        if exported.artifact.directory != expected:
            return (
                f"it is in {exported.artifact.directory}, not in the launch's export "
                f"directory {expected}"
            )
        if not export_leaf(self._common / self._export_root, expected, owner.launch):
            return (
                f"the launch's export directory {expected} is not one the controller makes "
                f"for launch {owner.launch} under {self._common / self._export_root}"
            )
        return ""

    def _import(
        self, exported: Exported, handle: LaunchHandle, git_input: LaunchGitInput
    ) -> Verified:
        problem = self._origin_problem(exported, handle)
        if problem:
            return Verified(
                refusal=f"the launch's exported artifact is refused: {problem}; nothing was "
                "read or published"
            )
        leaf = export_leaf(
            self._common / self._export_root, handle.inputs.export_dir, handle.inputs.owner.launch
        )
        try:
            with SafeRoot.open(self._common) as common:
                directory = common.subroot(f"{self._export_root}/{leaf}")
        except (StateError, OSError) as exc:
            return Verified(
                refusal="the launch's exported artifact is refused: its export directory "
                f"cannot be opened from the common dir without following a link "
                f"({type(exc).__name__}: {exc}); nothing was read or published"
            )
        with directory:
            try:
                imported = import_artifact(
                    self._common,
                    directory,
                    git_input,
                    self._runner,
                    read_commit=self._read_commit,
                    git_lock=self._git_lock,
                )
            except ObjectImportError as exc:
                return Verified(refusal=str(exc))
        return Verified(candidate=imported.candidate)

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


NOT_EXPORTED = Verified(
    refusal="the launch ran under a backend that seeds, so its candidate is its exported "
    "artifact (ADR 0013 D5), and it exported none: there is no candidate, the issue "
    "worktree's HEAD is never read in its place, and nothing was imported or published"
)
"""The verify outcome of a launch whose candidate is its artifact and that exported none."""


@dataclass(frozen=True)
class ImportedArtifact:
    """An exporting backend's candidate: the commit the verify step imported, and no other.

    ``verified`` is ``None`` when no verify step judged the launch's export:
    that is :data:`NOT_EXPORTED`'s refusal, never a fallback to the worktree.
    """

    verified: Verified | None

    def read(self) -> Candidate:
        if self.verified is None:
            return Candidate(False, "", NOT_EXPORTED.problem)
        if not self.verified.accepted:
            return Candidate(False, "", self.verified.problem)
        return Candidate(False, self.verified.candidate, "")
